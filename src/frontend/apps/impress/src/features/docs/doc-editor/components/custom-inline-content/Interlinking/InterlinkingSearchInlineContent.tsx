/* eslint-disable react-hooks/rules-of-hooks */
import { createReactInlineContentSpec } from '@blocknote/react';
import { useTreeContext } from '@gouvfr-lasuite/ui-kit';
import { TFunction } from 'i18next';
import { useRouter } from 'next/navigation';
import { useCallback } from 'react';

import { DocsBlockNoteEditor } from '@/docs/doc-editor';
import LinkPageIcon from '@/docs/doc-editor/assets/doc-link.svg';
import AddPageIcon from '@/docs/doc-editor/assets/doc-plus.svg';
import { Doc, useCreateChildDoc, useDocStore } from '@/docs/doc-management';

import { SearchPage } from './SearchPage';

export const InterlinkingSearchInlineContent = createReactInlineContentSpec(
  {
    type: 'interlinkingSearchInline',
    propSchema: {
      disabled: {
        default: false,
        values: [true, false],
      },
    },
    content: 'styled',
  },
  {
    render: (props) => {
      if (props.inlineContent.props.disabled) {
        return null;
      }

      return <SearchPage {...props} />;
    },
  },
);

export const getInterlinkingMenuItems = (
  editor: DocsBlockNoteEditor,
  t: TFunction<'translation', undefined>,
  group: string,
  createPage: () => void,
) => [
  {
    title: t('Link to a page'),
    onItemClick: () => {
      editor.insertInlineContent([
        {
          type: 'interlinkingSearchInline',
          props: {
            disabled: false,
          },
        },
      ]);
    },
    aliases: ['interlinking', 'link', 'anchor', 'a'],
    group,
    icon: <LinkPageIcon />,
    subtext: t('Link to a page'),
  },
  {
    title: t('New page'),
    onItemClick: createPage,
    aliases: ['new page'],
    group,
    icon: <AddPageIcon />,
    subtext: t('Add a new page'),
  },
];

export const useGetInterlinkingMenuItems = () => {
  const treeContext = useTreeContext<Doc>();
  const router = useRouter();
  const { currentDoc } = useDocStore();

  const { mutate: createChildDoc } = useCreateChildDoc({
    onSuccess: (createdDoc) => {
      const newDoc = {
        ...createdDoc,
        children: [],
        childrenCount: 0,
        parentId: currentDoc?.id ?? undefined,
      };
      treeContext?.treeData.addChild(currentDoc?.id || null, newDoc);

      router.push(`/docs/${newDoc.id}`);
      treeContext?.treeData.setSelectedNode(createdDoc);
    },
  });

  return useCallback(
    (editor: DocsBlockNoteEditor, t: TFunction<'translation', undefined>) =>
      getInterlinkingMenuItems(
        editor,
        t,
        t('Links'),
        () =>
          currentDoc?.id &&
          createChildDoc({
            parentId: currentDoc.id,
          }),
      ),
    [createChildDoc, currentDoc?.id],
  );
};
