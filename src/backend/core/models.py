"""
Declare and configure the models for the impress core application
"""
# pylint: disable=too-many-lines

import hashlib
import smtplib
import uuid
from datetime import timedelta
from logging import getLogger

from django.conf import settings
from django.contrib.auth import models as auth_models
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.postgres.fields import ArrayField
from django.contrib.sites.models import Site
from django.core import mail, validators
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.db import models, transaction
from django.db.models.functions import Left, Length
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import get_language, override
from django.utils.translation import gettext_lazy as _

from botocore.exceptions import ClientError
from rest_framework.exceptions import ValidationError
from timezone_field import TimeZoneField
from treebeard.mp_tree import MP_Node, MP_NodeManager, MP_NodeQuerySet

from .choices import (
    PRIVILEGED_ROLES,
    LinkReachChoices,
    LinkRoleChoices,
    RoleChoices,
    get_equivalent_link_definition,
)

logger = getLogger(__name__)


def get_trashbin_cutoff():
    """
    Calculate the cutoff datetime for soft-deleted items based on the retention policy.

    The function returns the current datetime minus the number of days specified in
    the TRASHBIN_CUTOFF_DAYS setting, indicating the oldest date for items that can
    remain in the trash bin.

    Returns:
        datetime: The cutoff datetime for soft-deleted items.
    """
    return timezone.now() - timedelta(days=settings.TRASHBIN_CUTOFF_DAYS)


class DuplicateEmailError(Exception):
    """Raised when an email is already associated with a pre-existing user."""

    def __init__(self, message=None, email=None):
        """Set message and email to describe the exception."""
        self.message = message
        self.email = email
        super().__init__(self.message)


class BaseModel(models.Model):
    """
    Serves as an abstract base model for other models, ensuring that records are validated
    before saving as Django doesn't do it by default.

    Includes fields common to all models: a UUID primary key and creation/update timestamps.
    """

    id = models.UUIDField(
        verbose_name=_("id"),
        help_text=_("primary key for the record as UUID"),
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    created_at = models.DateTimeField(
        verbose_name=_("created on"),
        help_text=_("date and time at which a record was created"),
        auto_now_add=True,
        editable=False,
    )
    updated_at = models.DateTimeField(
        verbose_name=_("updated on"),
        help_text=_("date and time at which a record was last updated"),
        auto_now=True,
        editable=False,
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        """Call `full_clean` before saving."""
        self.full_clean()
        super().save(*args, **kwargs)


class UserManager(auth_models.UserManager):
    """Custom manager for User model with additional methods."""

    def get_user_by_sub_or_email(self, sub, email):
        """Fetch existing user by sub or email."""
        try:
            return self.get(sub=sub)
        except self.model.DoesNotExist as err:
            if not email:
                return None

            if settings.OIDC_FALLBACK_TO_EMAIL_FOR_IDENTIFICATION:
                try:
                    return self.get(email=email)
                except self.model.DoesNotExist:
                    pass
            elif (
                self.filter(email=email).exists()
                and not settings.OIDC_ALLOW_DUPLICATE_EMAILS
            ):
                raise DuplicateEmailError(
                    _(
                        "We couldn't find a user with this sub but the email is already "
                        "associated with a registered user."
                    )
                ) from err
        return None


class User(AbstractBaseUser, BaseModel, auth_models.PermissionsMixin):
    """User model to work with OIDC only authentication."""

    sub_validator = validators.RegexValidator(
        regex=r"^[\w.@+-:]+\Z",
        message=_(
            "Enter a valid sub. This value may contain only letters, "
            "numbers, and @/./+/-/_/: characters."
        ),
    )

    sub = models.CharField(
        _("sub"),
        help_text=_(
            "Required. 255 characters or fewer. Letters, numbers, and @/./+/-/_/: characters only."
        ),
        max_length=255,
        unique=True,
        validators=[sub_validator],
        blank=True,
        null=True,
    )

    full_name = models.CharField(_("full name"), max_length=100, null=True, blank=True)
    short_name = models.CharField(_("short name"), max_length=20, null=True, blank=True)

    email = models.EmailField(_("identity email address"), blank=True, null=True)

    # Unlike the "email" field which stores the email coming from the OIDC token, this field
    # stores the email used by staff users to login to the admin site
    admin_email = models.EmailField(
        _("admin email address"), unique=True, blank=True, null=True
    )

    language = models.CharField(
        max_length=10,
        choices=settings.LANGUAGES,
        default=None,
        verbose_name=_("language"),
        help_text=_("The language in which the user wants to see the interface."),
        null=True,
        blank=True,
    )
    timezone = TimeZoneField(
        choices_display="WITH_GMT_OFFSET",
        use_pytz=False,
        default=settings.TIME_ZONE,
        help_text=_("The timezone in which the user wants to see times."),
    )
    is_device = models.BooleanField(
        _("device"),
        default=False,
        help_text=_("Whether the user is a device or a real user."),
    )
    is_staff = models.BooleanField(
        _("staff status"),
        default=False,
        help_text=_("Whether the user can log into this admin site."),
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_(
            "Whether this user should be treated as active. "
            "Unselect this instead of deleting accounts."
        ),
    )

    objects = UserManager()

    USERNAME_FIELD = "admin_email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "impress_user"
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self):
        return self.email or self.admin_email or str(self.id)

    def save(self, *args, **kwargs):
        """
        If it's a new user, give its user access to the documents to which s.he was invited.
        """
        is_adding = self._state.adding
        super().save(*args, **kwargs)

        if is_adding:
            self._convert_valid_invitations()

    def _convert_valid_invitations(self):
        """
        Convert valid invitations to document accesses.
        Expired invitations are ignored.
        """
        valid_invitations = Invitation.objects.filter(
            email=self.email,
            created_at__gte=(
                timezone.now()
                - timedelta(seconds=settings.INVITATION_VALIDITY_DURATION)
            ),
        ).select_related("document")

        if not valid_invitations.exists():
            return

        DocumentAccess.objects.bulk_create(
            [
                DocumentAccess(
                    user=self, document=invitation.document, role=invitation.role
                )
                for invitation in valid_invitations
            ]
        )

        # Set creator of documents if not yet set (e.g. documents created via server-to-server API)
        document_ids = [invitation.document_id for invitation in valid_invitations]
        Document.objects.filter(id__in=document_ids, creator__isnull=True).update(
            creator=self
        )

        valid_invitations.delete()

    def email_user(self, subject, message, from_email=None, **kwargs):
        """Email this user."""
        if not self.email:
            raise ValueError("User has no email address.")
        mail.send_mail(subject, message, from_email, [self.email], **kwargs)

    @cached_property
    def teams(self):
        """
        Get list of teams in which the user is, as a list of strings.
        Must be cached if retrieved remotely.
        """
        return []


class BaseAccess(BaseModel):
    """Base model for accesses to handle resources."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    team = models.CharField(max_length=100, blank=True)
    role = models.CharField(
        max_length=20, choices=RoleChoices.choices, default=RoleChoices.READER
    )

    class Meta:
        abstract = True


class DocumentQuerySet(MP_NodeQuerySet):
    """
    Custom queryset for the Document model, providing additional methods
    to filter documents based on user permissions.
    """

    def readable_per_se(self, user):
        """
        Filters the queryset to return documents on which the given user has
        direct access, team access or link access. This will not return all the
        documents that a user can read because it can be obtained via an ancestor.
        :param user: The user for whom readable documents are to be fetched.
        :return: A queryset of documents for which the user has direct access,
            team access or link access.
        """
        if user.is_authenticated:
            return self.filter(
                models.Q(accesses__user=user)
                | models.Q(accesses__team__in=user.teams)
                | ~models.Q(link_reach=LinkReachChoices.RESTRICTED)
            )

        return self.filter(link_reach=LinkReachChoices.PUBLIC)

    def annotate_is_favorite(self, user):
        """
        Annotate document queryset with the favorite status for the current user.
        """
        if user.is_authenticated:
            favorite_exists_subquery = DocumentFavorite.objects.filter(
                document_id=models.OuterRef("pk"), user=user
            )
            return self.annotate(is_favorite=models.Exists(favorite_exists_subquery))

        return self.annotate(is_favorite=models.Value(False))

    def annotate_user_roles(self, user):
        """
        Annotate document queryset with the roles of the current user
        on the document or its ancestors.
        """
        output_field = ArrayField(base_field=models.CharField())

        if user.is_authenticated:
            user_roles_subquery = DocumentAccess.objects.filter(
                models.Q(user=user) | models.Q(team__in=user.teams),
                document__path=Left(models.OuterRef("path"), Length("document__path")),
            ).values_list("role", flat=True)

            return self.annotate(
                user_roles=models.Func(
                    user_roles_subquery, function="ARRAY", output_field=output_field
                )
            )

        return self.annotate(
            user_roles=models.Value([], output_field=output_field),
        )


class DocumentManager(MP_NodeManager.from_queryset(DocumentQuerySet)):
    """
    Custom manager for the Document model, enabling the use of the custom
    queryset methods directly from the model manager.
    """

    def get_queryset(self):
        """Sets the custom queryset as the default."""
        return self._queryset_class(self.model).order_by("path")


# pylint: disable=too-many-public-methods
class Document(MP_Node, BaseModel):
    """Pad document carrying the content."""

    title = models.CharField(_("title"), max_length=255, null=True, blank=True)
    excerpt = models.TextField(_("excerpt"), max_length=300, null=True, blank=True)
    link_reach = models.CharField(
        max_length=20,
        choices=LinkReachChoices.choices,
        default=LinkReachChoices.RESTRICTED,
    )
    link_role = models.CharField(
        max_length=20, choices=LinkRoleChoices.choices, default=LinkRoleChoices.READER
    )
    creator = models.ForeignKey(
        User,
        on_delete=models.RESTRICT,
        related_name="documents_created",
        blank=True,
        null=True,
    )
    deleted_at = models.DateTimeField(null=True, blank=True)
    ancestors_deleted_at = models.DateTimeField(null=True, blank=True)
    has_deleted_children = models.BooleanField(default=False)
    duplicated_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="duplicates",
        editable=False,
        blank=True,
        null=True,
    )
    attachments = ArrayField(
        models.CharField(max_length=255),
        default=list,
        editable=False,
        blank=True,
        null=True,
    )

    _content = None

    # Tree structure
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    steplen = 7  # nb siblings max: 3,521,614,606,208
    node_order_by = []  # Manual ordering

    path = models.CharField(max_length=7 * 36, unique=True, db_collation="C")

    objects = DocumentManager()

    class Meta:
        db_table = "impress_document"
        ordering = ("path",)
        verbose_name = _("Document")
        verbose_name_plural = _("Documents")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(deleted_at__isnull=True)
                    | models.Q(deleted_at=models.F("ancestors_deleted_at"))
                ),
                name="check_deleted_at_matches_ancestors_deleted_at_when_set",
            ),
        ]

    def __str__(self):
        return str(self.title) if self.title else str(_("Untitled Document"))

    def __init__(self, *args, **kwargs):
        """Initialize cache property."""
        super().__init__(*args, **kwargs)
        self._ancestors_link_definition = None
        self._computed_link_definition = None

    def save(self, *args, **kwargs):
        """Write content to object storage only if _content has changed."""
        super().save(*args, **kwargs)

        if self._content:
            file_key = self.file_key
            bytes_content = self._content.encode("utf-8")

            # Attempt to directly check if the object exists using the storage client.
            try:
                response = default_storage.connection.meta.client.head_object(
                    Bucket=default_storage.bucket_name, Key=file_key
                )
            except ClientError as excpt:
                # If the error is a 404, the object doesn't exist, so we should create it.
                if excpt.response["Error"]["Code"] == "404":
                    has_changed = True
                else:
                    raise
            else:
                # Compare the existing ETag with the MD5 hash of the new content.
                has_changed = (
                    response["ETag"].strip('"')
                    != hashlib.md5(bytes_content).hexdigest()  # noqa: S324
                )

            if has_changed:
                content_file = ContentFile(bytes_content)
                default_storage.save(file_key, content_file)

    def is_leaf(self):
        """
        :returns: True if the node is has no children
        """
        return not self.has_deleted_children and self.numchild == 0

    @property
    def key_base(self):
        """Key base of the location where the document is stored in object storage."""
        if not self.pk:
            raise RuntimeError(
                "The document instance must be saved before requesting a storage key."
            )
        return str(self.pk)

    @property
    def file_key(self):
        """Key of the object storage file to which the document content is stored"""
        return f"{self.key_base}/file"

    @property
    def content(self):
        """Return the json content from object storage if available"""
        if self._content is None and self.id:
            try:
                response = self.get_content_response()
            except (FileNotFoundError, ClientError):
                pass
            else:
                self._content = response["Body"].read().decode("utf-8")
        return self._content

    @content.setter
    def content(self, content):
        """Cache the content, don't write to object storage yet"""
        if not isinstance(content, str):
            raise ValueError("content should be a string.")

        self._content = content

    def get_content_response(self, version_id=""):
        """Get the content in a specific version of the document"""
        params = {
            "Bucket": default_storage.bucket_name,
            "Key": self.file_key,
        }
        if version_id:
            params["VersionId"] = version_id
        return default_storage.connection.meta.client.get_object(**params)

    def get_versions_slice(self, from_version_id="", min_datetime=None, page_size=None):
        """Get document versions from object storage with pagination and starting conditions"""
        # /!\ Trick here /!\
        # The "KeyMarker" and "VersionIdMarker" fields must either be both set or both not set.
        # The error we get otherwise is not helpful at all.
        markers = {}
        if from_version_id:
            markers.update(
                {"KeyMarker": self.file_key, "VersionIdMarker": from_version_id}
            )

        real_page_size = (
            min(page_size, settings.DOCUMENT_VERSIONS_PAGE_SIZE)
            if page_size
            else settings.DOCUMENT_VERSIONS_PAGE_SIZE
        )

        response = default_storage.connection.meta.client.list_object_versions(
            Bucket=default_storage.bucket_name,
            Prefix=self.file_key,
            # compensate the latest version that we exclude below and get one more to
            # know if there are more pages
            MaxKeys=real_page_size + 2,
            **markers,
        )

        min_last_modified = min_datetime or self.created_at
        versions = [
            {
                key_snake: version[key_camel]
                for key_snake, key_camel in [
                    ("etag", "ETag"),
                    ("is_latest", "IsLatest"),
                    ("last_modified", "LastModified"),
                    ("version_id", "VersionId"),
                ]
            }
            for version in response.get("Versions", [])
            if version["LastModified"] >= min_last_modified
            and version["IsLatest"] is False
        ]
        results = versions[:real_page_size]

        count = len(results)
        if count == len(versions):
            is_truncated = False
            next_version_id_marker = ""
        else:
            is_truncated = True
            next_version_id_marker = versions[count - 1]["version_id"]

        return {
            "next_version_id_marker": next_version_id_marker,
            "is_truncated": is_truncated,
            "versions": results,
            "count": count,
        }

    def delete_version(self, version_id):
        """Delete a version from object storage given its version id"""
        return default_storage.connection.meta.client.delete_object(
            Bucket=default_storage.bucket_name, Key=self.file_key, VersionId=version_id
        )

    def get_nb_accesses_cache_key(self):
        """Generate a unique cache key for each document."""
        return f"document_{self.id!s}_nb_accesses"

    def get_nb_accesses(self):
        """
        Calculate the number of accesses:
        - directly attached to the document
        - attached to any of the document's ancestors
        """
        cache_key = self.get_nb_accesses_cache_key()
        nb_accesses = cache.get(cache_key)

        if nb_accesses is None:
            nb_accesses = (
                DocumentAccess.objects.filter(document=self).count(),
                DocumentAccess.objects.filter(
                    document__path=Left(
                        models.Value(self.path), Length("document__path")
                    ),
                    document__ancestors_deleted_at__isnull=True,
                ).count(),
            )
            cache.set(cache_key, nb_accesses)

        return nb_accesses

    @property
    def nb_accesses_direct(self):
        """Returns the number of accesses related to the document or one of its ancestors."""
        return self.get_nb_accesses()[0]

    @property
    def nb_accesses_ancestors(self):
        """Returns the number of accesses related to the document or one of its ancestors."""
        return self.get_nb_accesses()[1]

    def invalidate_nb_accesses_cache(self):
        """
        Invalidate the cache for number of accesses, including on affected descendants.
        Args:
            path: can optionally be passed as argument (useful when invalidating cache for a
                document we just deleted)
        """

        for document in Document.objects.filter(path__startswith=self.path).only("id"):
            cache_key = document.get_nb_accesses_cache_key()
            cache.delete(cache_key)

    def get_role(self, user):
        """Return the roles a user has on a document."""
        if not user.is_authenticated:
            return None

        try:
            roles = self.user_roles or []
        except AttributeError:
            roles = DocumentAccess.objects.filter(
                models.Q(user=user) | models.Q(team__in=user.teams),
                document__path=Left(models.Value(self.path), Length("document__path")),
            ).values_list("role", flat=True)

        return RoleChoices.max(*roles)

    def compute_ancestors_links_paths_mapping(self):
        """
        Compute the ancestors links for the current document up to the highest readable ancestor.
        """
        ancestors = (
            (self.get_ancestors() | self._meta.model.objects.filter(pk=self.pk))
            .filter(ancestors_deleted_at__isnull=True)
            .order_by("path")
        )
        ancestors_links = []
        paths_links_mapping = {}

        for ancestor in ancestors:
            ancestors_links.append(
                {"link_reach": ancestor.link_reach, "link_role": ancestor.link_role}
            )
            paths_links_mapping[ancestor.path] = ancestors_links.copy()

        return paths_links_mapping

    @property
    def link_definition(self):
        """Returns link reach/role as a definition in dictionary format."""
        return {"link_reach": self.link_reach, "link_role": self.link_role}

    @property
    def ancestors_link_definition(self):
        """Link definition equivalent to all document's ancestors."""
        if getattr(self, "_ancestors_link_definition", None) is None:
            if self.depth <= 1:
                ancestors_links = []
            else:
                mapping = self.compute_ancestors_links_paths_mapping()
                ancestors_links = mapping.get(self.path[: -self.steplen], [])
            self._ancestors_link_definition = get_equivalent_link_definition(
                ancestors_links
            )

        return self._ancestors_link_definition

    @ancestors_link_definition.setter
    def ancestors_link_definition(self, definition):
        """Cache the ancestors_link_definition."""
        self._ancestors_link_definition = definition

    @property
    def ancestors_link_reach(self):
        """Link reach equivalent to all document's ancestors."""
        return self.ancestors_link_definition["link_reach"]

    @property
    def ancestors_link_role(self):
        """Link role equivalent to all document's ancestors."""
        return self.ancestors_link_definition["link_role"]

    @property
    def computed_link_definition(self):
        """
        Link reach/role on the document, combining inherited ancestors' link
        definitions and the document's own link definition.
        """
        if getattr(self, "_computed_link_definition", None) is None:
            self._computed_link_definition = get_equivalent_link_definition(
                [self.ancestors_link_definition, self.link_definition]
            )
        return self._computed_link_definition

    @property
    def computed_link_reach(self):
        """Actual link reach on the document."""
        return self.computed_link_definition["link_reach"]

    @property
    def computed_link_role(self):
        """Actual link role on the document."""
        return self.computed_link_definition["link_role"]

    def get_abilities(self, user):
        """
        Compute and return abilities for a given user on the document.
        """
        # First get the role based on specific access
        role = self.get_role(user)

        # Characteristics that are based only on specific access
        is_owner = role == RoleChoices.OWNER
        is_deleted = self.ancestors_deleted_at and not is_owner
        is_owner_or_admin = (is_owner or role == RoleChoices.ADMIN) and not is_deleted

        # Compute access roles before adding link roles because we don't
        # want anonymous users to access versions (we wouldn't know from
        # which date to allow them anyway)
        # Anonymous users should also not see document accesses
        has_access_role = bool(role) and not is_deleted
        can_update_from_access = (
            is_owner_or_admin or role == RoleChoices.EDITOR
        ) and not is_deleted

        link_select_options = LinkReachChoices.get_select_options(
            **self.ancestors_link_definition
        )
        link_definition = get_equivalent_link_definition(
            [
                self.ancestors_link_definition,
                {"link_reach": self.link_reach, "link_role": self.link_role},
            ]
        )

        link_reach = link_definition["link_reach"]
        if link_reach == LinkReachChoices.PUBLIC or (
            link_reach == LinkReachChoices.AUTHENTICATED and user.is_authenticated
        ):
            role = RoleChoices.max(role, link_definition["link_role"])

        can_get = bool(role) and not is_deleted
        can_update = (
            is_owner_or_admin or role == RoleChoices.EDITOR
        ) and not is_deleted

        ai_allow_reach_from = settings.AI_ALLOW_REACH_FROM
        ai_access = any(
            [
                ai_allow_reach_from == LinkReachChoices.PUBLIC and can_update,
                ai_allow_reach_from == LinkReachChoices.AUTHENTICATED
                and user.is_authenticated
                and can_update,
                ai_allow_reach_from == LinkReachChoices.RESTRICTED
                and can_update_from_access,
            ]
        )

        return {
            "accesses_manage": is_owner_or_admin,
            "accesses_view": has_access_role,
            "ai_transform": ai_access,
            "ai_translate": ai_access,
            "attachment_upload": can_update,
            "media_check": can_get,
            "can_edit": can_update,
            "children_list": can_get,
            "children_create": can_update and user.is_authenticated,
            "collaboration_auth": can_get,
            "cors_proxy": can_get,
            "descendants": can_get,
            "destroy": is_owner,
            "duplicate": can_get and user.is_authenticated,
            "favorite": can_get and user.is_authenticated,
            "link_configuration": is_owner_or_admin,
            "invite_owner": is_owner,
            "move": is_owner_or_admin and not self.ancestors_deleted_at,
            "partial_update": can_update,
            "restore": is_owner,
            "retrieve": can_get,
            "media_auth": can_get,
            "link_select_options": link_select_options,
            "tree": can_get,
            "update": can_update,
            "versions_destroy": is_owner_or_admin,
            "versions_list": has_access_role,
            "versions_retrieve": has_access_role,
        }

    def send_email(self, subject, emails, context=None, language=None):
        """Generate and send email from a template."""
        context = context or {}
        domain = Site.objects.get_current().domain
        language = language or get_language()
        context.update(
            {
                "brandname": settings.EMAIL_BRAND_NAME,
                "document": self,
                "domain": domain,
                "link": f"{domain}/docs/{self.id}/",
                "document_title": self.title or str(_("Untitled Document")),
                "logo_img": settings.EMAIL_LOGO_IMG,
            }
        )

        with override(language):
            msg_html = render_to_string("mail/html/template.html", context)
            msg_plain = render_to_string("mail/text/template.txt", context)
            subject = str(subject)  # Force translation

            try:
                send_mail(
                    subject.capitalize(),
                    msg_plain,
                    settings.EMAIL_FROM,
                    emails,
                    html_message=msg_html,
                    fail_silently=False,
                )
            except smtplib.SMTPException as exception:
                logger.error("invitation to %s was not sent: %s", emails, exception)

    def send_invitation_email(self, email, role, sender, language=None):
        """Method allowing a user to send an email invitation to another user for a document."""
        language = language or get_language()
        role = RoleChoices(role).label
        sender_name = sender.full_name or sender.email
        sender_name_email = (
            f"{sender.full_name:s} ({sender.email})"
            if sender.full_name
            else sender.email
        )

        with override(language):
            context = {
                "title": _("{name} shared a document with you!").format(
                    name=sender_name
                ),
                "message": _(
                    '{name} invited you with the role "{role}" on the following document:'
                ).format(name=sender_name_email, role=role.lower()),
            }
            subject = (
                context["title"]
                if not self.title
                else _("{name} shared a document with you: {title}").format(
                    name=sender_name, title=self.title
                )
            )

        self.send_email(subject, [email], context, language)

    @transaction.atomic
    def soft_delete(self):
        """
        Soft delete the document, marking the deletion on descendants.
        We still keep the .delete() method untouched for programmatic purposes.
        """
        if (
            self._meta.model.objects.filter(
                models.Q(deleted_at__isnull=False)
                | models.Q(ancestors_deleted_at__isnull=False),
                pk=self.pk,
            ).exists()
            or self.get_ancestors().filter(deleted_at__isnull=False).exists()
        ):
            raise RuntimeError(
                "This document is already deleted or has deleted ancestors."
            )

        self.ancestors_deleted_at = self.deleted_at = timezone.now()
        self.save()
        self.invalidate_nb_accesses_cache()

        if self.depth > 1:
            self._meta.model.objects.filter(pk=self.get_parent().pk).update(
                numchild=models.F("numchild") - 1,
                has_deleted_children=True,
            )

        # Mark all descendants as soft deleted
        self.get_descendants().filter(ancestors_deleted_at__isnull=True).update(
            ancestors_deleted_at=self.ancestors_deleted_at
        )

    @transaction.atomic
    def restore(self):
        """Cancelling a soft delete with checks."""
        # This should not happen
        if self._meta.model.objects.filter(
            pk=self.pk, deleted_at__isnull=True
        ).exists():
            raise RuntimeError("This document is not deleted.")

        if self.deleted_at < get_trashbin_cutoff():
            raise RuntimeError(
                "This document was permanently deleted and cannot be restored."
            )

        # save the current deleted_at value to exclude it from the descendants update
        current_deleted_at = self.deleted_at

        # Restore the current document
        self.deleted_at = None

        # Calculate the minimum `deleted_at` among all ancestors
        ancestors_deleted_at = (
            self.get_ancestors()
            .filter(deleted_at__isnull=False)
            .order_by("deleted_at")
            .values_list("deleted_at", flat=True)
            .first()
        )
        self.ancestors_deleted_at = ancestors_deleted_at
        self.save(update_fields=["deleted_at", "ancestors_deleted_at"])
        self.invalidate_nb_accesses_cache()

        self.get_descendants().exclude(
            models.Q(deleted_at__isnull=False)
            | models.Q(ancestors_deleted_at__lt=current_deleted_at)
        ).update(ancestors_deleted_at=self.ancestors_deleted_at)

        if self.depth > 1:
            self._meta.model.objects.filter(pk=self.get_parent().pk).update(
                numchild=models.F("numchild") + 1
            )


class LinkTrace(BaseModel):
    """
    Relation model to trace accesses to a document via a link by a logged-in user.
    This is necessary to show the document in the user's list of documents even
    though the user does not have a role on the document.
    """

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="link_traces",
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="link_traces")

    class Meta:
        db_table = "impress_link_trace"
        verbose_name = _("Document/user link trace")
        verbose_name_plural = _("Document/user link traces")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document"],
                name="unique_link_trace_document_user",
                violation_error_message=_(
                    "A link trace already exists for this document/user."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.user!s} trace on document {self.document!s}"


class DocumentFavorite(BaseModel):
    """Relation model to store a user's favorite documents."""

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="favorited_by_users",
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="favorite_documents"
    )

    class Meta:
        db_table = "impress_document_favorite"
        verbose_name = _("Document favorite")
        verbose_name_plural = _("Document favorites")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document"],
                name="unique_document_favorite_user",
                violation_error_message=_(
                    "This document is already targeted by a favorite relation instance "
                    "for the same user."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.user!s} favorite on document {self.document!s}"


class DocumentAccess(BaseAccess):
    """Relation model to give access to a document for a user or a team with a role."""

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="accesses",
    )

    class Meta:
        db_table = "impress_document_access"
        ordering = ("-created_at",)
        verbose_name = _("Document/user relation")
        verbose_name_plural = _("Document/user relations")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document"],
                condition=models.Q(user__isnull=False),  # Exclude null users
                name="unique_document_user",
                violation_error_message=_("This user is already in this document."),
            ),
            models.UniqueConstraint(
                fields=["team", "document"],
                condition=models.Q(team__gt=""),  # Exclude empty string teams
                name="unique_document_team",
                violation_error_message=_("This team is already in this document."),
            ),
            models.CheckConstraint(
                condition=models.Q(user__isnull=False, team="")
                | models.Q(user__isnull=True, team__gt=""),
                name="check_document_access_either_user_or_team",
                violation_error_message=_("Either user or team must be set, not both."),
            ),
        ]

    def __str__(self):
        return f"{self.user!s} is {self.role:s} in document {self.document!s}"

    def save(self, *args, **kwargs):
        """Override save to clear the document's cache for number of accesses."""
        super().save(*args, **kwargs)
        self.document.invalidate_nb_accesses_cache()

    @property
    def target_key(self):
        """Get a unique key for the actor targeted by the access, without possible conflict."""
        return f"user:{self.user_id!s}" if self.user_id else f"team:{self.team:s}"

    def delete(self, *args, **kwargs):
        """Override delete to clear the document's cache for number of accesses."""
        super().delete(*args, **kwargs)
        self.document.invalidate_nb_accesses_cache()

    def set_user_roles_tuple(self, ancestors_role, current_role):
        """
        Set a precomputed (ancestor_role, current_role) tuple for this instance.

        This avoids querying the database in `get_roles_tuple()` and is useful
        when roles are already known, such as in bulk serialization.

        Args:
            ancestor_role (str | None): Highest role on any ancestor document.
            current_role (str | None): Role on the current document.
        """
        # pylint: disable=attribute-defined-outside-init
        self._prefetched_user_roles_tuple = (ancestors_role, current_role)

    def get_user_roles_tuple(self, user):
        """
        Return a tuple of:
        - the highest role the user has on any ancestor of the document
        - the role the user has on the current document

        If roles have been explicitly set using `set_user_roles_tuple()`,
        those will be returned instead of querying the database.

        This allows viewsets or serializers to precompute roles for performance
        when handling multiple documents at once.

        Args:
            user (User): The user whose roles are being evaluated.

        Returns:
            tuple[str | None, str | None]: (max_ancestor_role, current_document_role)
        """
        if not user.is_authenticated:
            return None, None

        try:
            return self._prefetched_user_roles_tuple
        except AttributeError:
            pass

        ancestors = (
            self.document.get_ancestors() | Document.objects.filter(pk=self.document_id)
        ).filter(ancestors_deleted_at__isnull=True)

        access_tuples = DocumentAccess.objects.filter(
            models.Q(user=user) | models.Q(team__in=user.teams),
            document__in=ancestors,
        ).values_list("document_id", "role")

        ancestors_roles = []
        current_roles = []
        for doc_id, role in access_tuples:
            if doc_id == self.document_id:
                current_roles.append(role)
            else:
                ancestors_roles.append(role)

        return RoleChoices.max(*ancestors_roles), RoleChoices.max(*current_roles)

    def get_abilities(self, user):
        """
        Compute and return abilities for a given user on the document access.
        """
        ancestors_role, current_role = self.get_user_roles_tuple(user)
        role = RoleChoices.max(ancestors_role, current_role)
        is_owner_or_admin = role in PRIVILEGED_ROLES

        if self.role == RoleChoices.OWNER:
            can_delete = role == RoleChoices.OWNER and (
                # check if document is not root trying to avoid an extra query
                self.document.depth > 1
                or DocumentAccess.objects.filter(
                    document_id=self.document_id, role=RoleChoices.OWNER
                ).count()
                > 1
            )
            set_role_to = RoleChoices.values if can_delete else []
        else:
            can_delete = is_owner_or_admin
            set_role_to = []
            if is_owner_or_admin:
                set_role_to.extend(
                    [RoleChoices.READER, RoleChoices.EDITOR, RoleChoices.ADMIN]
                )
            if role == RoleChoices.OWNER:
                set_role_to.append(RoleChoices.OWNER)

        # Filter out roles that would be lower than the one the user already has
        ancestors_role_priority = RoleChoices.get_priority(
            getattr(self, "max_ancestors_role", None)
        )
        set_role_to = [
            candidate_role
            for candidate_role in set_role_to
            if RoleChoices.get_priority(candidate_role) >= ancestors_role_priority
        ]
        if len(set_role_to) == 1:
            set_role_to = []

        return {
            "destroy": can_delete,
            "update": bool(set_role_to) and is_owner_or_admin,
            "partial_update": bool(set_role_to) and is_owner_or_admin,
            "retrieve": (self.user and self.user.id == user.id) or is_owner_or_admin,
            "set_role_to": set_role_to,
        }


class DocumentAskForAccess(BaseModel):
    """Relation model to ask for access to a document."""

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="ask_for_accesses"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ask_for_accesses"
    )

    role = models.CharField(
        max_length=20, choices=RoleChoices.choices, default=RoleChoices.READER
    )

    class Meta:
        db_table = "impress_document_ask_for_access"
        verbose_name = _("Document ask for access")
        verbose_name_plural = _("Document ask for accesses")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "document"],
                name="unique_document_ask_for_access_user",
                violation_error_message=_(
                    "This user has already asked for access to this document."
                ),
            ),
        ]

    def __str__(self):
        return f"{self.user!s} asked for access to document {self.document!s}"

    def get_abilities(self, user):
        """Compute and return abilities for a given user."""
        roles = []

        if user.is_authenticated:
            teams = user.teams
            try:
                roles = self.user_roles or []
            except AttributeError:
                try:
                    roles = self.document.accesses.filter(
                        models.Q(user=user) | models.Q(team__in=teams),
                    ).values_list("role", flat=True)
                except (self._meta.model.DoesNotExist, IndexError):
                    roles = []

        is_admin_or_owner = bool(
            set(roles).intersection({RoleChoices.OWNER, RoleChoices.ADMIN})
        )

        return {
            "destroy": is_admin_or_owner,
            "update": is_admin_or_owner,
            "partial_update": is_admin_or_owner,
            "retrieve": is_admin_or_owner,
            "accept": is_admin_or_owner,
        }

    def accept(self, role=None):
        """Accept a document ask for access resource."""
        if role is None:
            role = self.role

        DocumentAccess.objects.update_or_create(
            document=self.document,
            user=self.user,
            defaults={"role": role},
            create_defaults={"role": role},
        )
        self.delete()

    def send_ask_for_access_email(self, email, language=None):
        """
        Method allowing a user to send an email notification when asking for access to a document.
        """

        language = language or get_language()
        sender = self.user
        sender_name = sender.full_name or sender.email
        sender_name_email = (
            f"{sender.full_name:s} ({sender.email})"
            if sender.full_name
            else sender.email
        )

        with override(language):
            context = {
                "title": _("{name} would like access to a document!").format(
                    name=sender_name
                ),
                "message": _(
                    "{name} would like access to the following document:"
                ).format(name=sender_name_email),
            }
            subject = (
                context["title"]
                if not self.document.title
                else _("{name} is asking for access to the document: {title}").format(
                    name=sender_name, title=self.document.title
                )
            )

        self.document.send_email(subject, [email], context, language)


class Template(BaseModel):
    """HTML and CSS code used for formatting the print around the MarkDown body."""

    title = models.CharField(_("title"), max_length=255)
    description = models.TextField(_("description"), blank=True)
    code = models.TextField(_("code"), blank=True)
    css = models.TextField(_("css"), blank=True)
    is_public = models.BooleanField(
        _("public"),
        default=False,
        help_text=_("Whether this template is public for anyone to use."),
    )

    class Meta:
        db_table = "impress_template"
        ordering = ("title",)
        verbose_name = _("Template")
        verbose_name_plural = _("Templates")

    def __str__(self):
        return self.title

    def get_role(self, user):
        """Return the roles a user has on a resource as an iterable."""
        if not user.is_authenticated:
            return None

        try:
            roles = self.user_roles or []
        except AttributeError:
            try:
                roles = self.accesses.filter(
                    models.Q(user=user) | models.Q(team__in=user.teams),
                ).values_list("role", flat=True)
            except (models.ObjectDoesNotExist, IndexError):
                roles = []

        return RoleChoices.max(*roles)

    def get_abilities(self, user):
        """
        Compute and return abilities for a given user on the template.
        """
        role = self.get_role(user)
        is_owner_or_admin = role in PRIVILEGED_ROLES
        can_get = self.is_public or bool(role)
        can_update = is_owner_or_admin or role == RoleChoices.EDITOR

        return {
            "destroy": role == RoleChoices.OWNER,
            "generate_document": can_get,
            "accesses_manage": is_owner_or_admin,
            "update": can_update,
            "partial_update": can_update,
            "retrieve": can_get,
        }


class TemplateAccess(BaseAccess):
    """Relation model to give access to a template for a user or a team with a role."""

    template = models.ForeignKey(
        Template,
        on_delete=models.CASCADE,
        related_name="accesses",
    )

    class Meta:
        db_table = "impress_template_access"
        ordering = ("-created_at",)
        verbose_name = _("Template/user relation")
        verbose_name_plural = _("Template/user relations")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "template"],
                condition=models.Q(user__isnull=False),  # Exclude null users
                name="unique_template_user",
                violation_error_message=_("This user is already in this template."),
            ),
            models.UniqueConstraint(
                fields=["team", "template"],
                condition=models.Q(team__gt=""),  # Exclude empty string teams
                name="unique_template_team",
                violation_error_message=_("This team is already in this template."),
            ),
            models.CheckConstraint(
                condition=models.Q(user__isnull=False, team="")
                | models.Q(user__isnull=True, team__gt=""),
                name="check_template_access_either_user_or_team",
                violation_error_message=_("Either user or team must be set, not both."),
            ),
        ]

    def __str__(self):
        return f"{self.user!s} is {self.role:s} in template {self.template!s}"

    def get_role(self, user):
        """
        Get the role a user has on a resource.
        """
        if not user.is_authenticated:
            return None

        try:
            roles = self.user_roles or []
        except AttributeError:
            teams = user.teams
            try:
                roles = self.template.accesses.filter(
                    models.Q(user=user) | models.Q(team__in=teams),
                ).values_list("role", flat=True)
            except (Template.DoesNotExist, IndexError):
                roles = []

        return RoleChoices.max(*roles)

    def get_abilities(self, user):
        """
        Compute and return abilities for a given user on the template access.
        """
        role = self.get_role(user)
        is_owner_or_admin = role in PRIVILEGED_ROLES

        if self.role == RoleChoices.OWNER:
            can_delete = (role == RoleChoices.OWNER) and self.template.accesses.filter(
                role=RoleChoices.OWNER
            ).count() > 1
            set_role_to = (
                [RoleChoices.ADMIN, RoleChoices.EDITOR, RoleChoices.READER]
                if can_delete
                else []
            )
        else:
            can_delete = is_owner_or_admin
            set_role_to = []
            if role == RoleChoices.OWNER:
                set_role_to.append(RoleChoices.OWNER)
            if is_owner_or_admin:
                set_role_to.extend(
                    [RoleChoices.ADMIN, RoleChoices.EDITOR, RoleChoices.READER]
                )

        # Remove the current role as we don't want to propose it as an option
        try:
            set_role_to.remove(self.role)
        except ValueError:
            pass

        return {
            "destroy": can_delete,
            "update": bool(set_role_to),
            "partial_update": bool(set_role_to),
            "retrieve": bool(role),
            "set_role_to": set_role_to,
        }


class Invitation(BaseModel):
    """User invitation to a document."""

    email = models.EmailField(_("email address"), null=False, blank=False)
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    role = models.CharField(
        max_length=20, choices=RoleChoices.choices, default=RoleChoices.READER
    )
    issuer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="invitations",
        blank=True,
        null=True,
    )

    class Meta:
        db_table = "impress_invitation"
        verbose_name = _("Document invitation")
        verbose_name_plural = _("Document invitations")
        constraints = [
            models.UniqueConstraint(
                fields=["email", "document"], name="email_and_document_unique_together"
            )
        ]

    def __str__(self):
        return f"{self.email} invited to {self.document}"

    def clean(self):
        """Validate fields."""
        super().clean()

        # Check if an identity already exists for the provided email
        if (
            User.objects.filter(email=self.email).exists()
            and not settings.OIDC_ALLOW_DUPLICATE_EMAILS
        ):
            raise ValidationError(
                {"email": [_("This email is already associated to a registered user.")]}
            )

    @property
    def is_expired(self):
        """Calculate if invitation is still valid or has expired."""
        if not self.created_at:
            return None

        validity_duration = timedelta(seconds=settings.INVITATION_VALIDITY_DURATION)
        return timezone.now() > (self.created_at + validity_duration)

    def get_abilities(self, user):
        """Compute and return abilities for a given user."""
        roles = []

        if user.is_authenticated:
            teams = user.teams
            try:
                roles = self.user_roles or []
            except AttributeError:
                try:
                    roles = self.document.accesses.filter(
                        models.Q(user=user) | models.Q(team__in=teams),
                    ).values_list("role", flat=True)
                except (self._meta.model.DoesNotExist, IndexError):
                    roles = []

        is_admin_or_owner = bool(
            set(roles).intersection({RoleChoices.OWNER, RoleChoices.ADMIN})
        )

        return {
            "destroy": is_admin_or_owner,
            "update": is_admin_or_owner,
            "partial_update": is_admin_or_owner,
            "retrieve": is_admin_or_owner,
        }
