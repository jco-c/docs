import { useTreeContext } from '@gouvfr-lasuite/ui-kit';
import { TFunction } from 'i18next';
import { useRouter } from 'next/navigation';
import { useCallback } from 'react';

import AddPageIcon from '@/docs/doc-editor/assets/doc-plus.svg';
import { Doc, useCreateChildDoc, useDocStore } from '@/docs/doc-management';

export const getInterlinkingMenuItems = (
  t: TFunction<'translation', undefined>,
  group: string,
  createPage: () => void,
) => [
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
    (t: TFunction<'translation', undefined>) =>
      getInterlinkingMenuItems(
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
