/* eslint-disable react-hooks/rules-of-hooks */
import {
  PartialCustomInlineContentFromConfig,
  StyleSchema,
} from '@blocknote/core';
import { useBlockNoteEditor } from '@blocknote/react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { css } from 'styled-components';

import {
  Box,
  BoxButton,
  Card,
  Icon,
  QuickSearch,
  QuickSearchItemContent,
  Text,
} from '@/components';
import { useCunninghamTheme } from '@/cunningham';
import {
  DocsBlockSchema,
  DocsInlineContentSchema,
  DocsStyleSchema,
} from '@/docs/doc-editor';
import FoundPageIcon from '@/docs/doc-editor/assets/doc-found.svg';
import AddPageIcon from '@/docs/doc-editor/assets/doc-plus.svg';
import { useCreateChildDocTree, useDocStore } from '@/docs/doc-management';
import { DocSearchSubPageContent, DocSearchTarget } from '@/docs/doc-search';

const inputStyle = css`
  background-color: var(--c--theme--colors--greyscale-100);
  border: none;
  outline: none;
  color: var(--c--theme--colors--greyscale-700);
  font-size: 16px;
  width: 100%;
  font-family: 'Inter';
`;

type SearchPageProps = {
  updateInlineContent: (
    update: PartialCustomInlineContentFromConfig<
      {
        type: string;
        propSchema: {
          disabled: {
            default: boolean;
          };
        };
        content: 'styled';
      },
      StyleSchema
    >,
  ) => void;
  contentRef: (node: HTMLElement | null) => void;
};

export const SearchPage = ({
  contentRef,
  updateInlineContent,
}: SearchPageProps) => {
  const { colorsTokens } = useCunninghamTheme();
  const editor = useBlockNoteEditor<
    DocsBlockSchema,
    DocsInlineContentSchema,
    DocsStyleSchema
  >();
  const { t } = useTranslation();
  const { currentDoc } = useDocStore();
  const createChildDoc = useCreateChildDocTree(currentDoc?.id);
  const inputRef = useRef<HTMLInputElement>(null);
  const [search, setSearch] = useState('');

  /**
   * createReactInlineContentSpec add automatically the focus after
   * the inline content, so we need to set the focus on the input
   * after the component is mounted.
   */
  useEffect(() => {
    setTimeout(() => {
      if (inputRef.current) {
        inputRef.current.focus();
      }
    }, 100);
  }, [inputRef]);

  return (
    <Box as="span" $position="relative">
      <Box
        as="span"
        className="inline-content"
        $background={colorsTokens['greyscale-100']}
        $color="var(--c--theme--colors--greyscale-700)"
        $direction="row"
        $radius="3px"
        $padding="1px"
        $display="inline-flex"
        tabIndex={-1} // Ensure the span is focusable
      >
        {' '}
        /
        <Box
          as="input"
          $padding={{ left: '3px' }}
          $css={inputStyle}
          ref={inputRef}
          $display="inline-flex"
          onInput={(e) => {
            const value = (e.target as HTMLInputElement).value;
            setSearch(value);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Backspace' && search.length === 0) {
              e.preventDefault();

              updateInlineContent({
                type: 'interlinkingSearchInline',
                props: {
                  disabled: true,
                },
              });

              contentRef(null);
              editor.focus();
              editor.insertInlineContent(['']);
            }
          }}
        />
      </Box>
      <Box
        $minWidth="220px"
        $width="fit-content"
        $position="absolute"
        $css={css`
          top: 28px;
          z-index: 1000;
        `}
      >
        <QuickSearch showInput={false}>
          <Card
            $css={css`
              box-shadow: 0 0 3px 0px var(--c--theme--colors--greyscale-200);
              & > div {
                margin-top: 0;
                & [cmdk-group-heading] {
                  padding: 0.4rem;
                  margin: 0;
                }

                & [cmdk-group-items] .ml-b {
                  margin-left: 0rem;
                  padding: 0.5rem;
                  font-size: 14px;
                  display: block;
                }

                & [cmdk-item] {
                  border-radius: 0;
                }

                & .--docs--doc-search-item > div {
                  gap: 0.8rem;
                }
              }
            `}
            $margin={{ top: '0.5rem' }}
          >
            <DocSearchSubPageContent
              search={search}
              filters={{ target: DocSearchTarget.CURRENT }}
              onSelect={(doc) => {
                updateInlineContent({
                  type: 'interlinkingSearchInline',
                  props: {
                    disabled: true,
                  },
                });

                editor.insertInlineContent([
                  {
                    type: 'interlinkingLinkInline',
                    props: {
                      url: `/docs/${doc.id}`,
                      title: doc.title || '',
                    },
                  },
                  ' ',
                ]);
              }}
              renderElement={(doc) => (
                <QuickSearchItemContent
                  left={
                    <Box
                      $direction="row"
                      $gap="0.6rem"
                      $align="center"
                      $padding={{ vertical: '0.5rem', horizontal: '0.2rem' }}
                      $width="100%"
                    >
                      <FoundPageIcon />
                      <Text
                        $size="14px"
                        $color="var(--c--theme--colors--greyscale-1000)"
                        spellCheck="false"
                      >
                        {doc.title}
                      </Text>
                    </Box>
                  }
                  right={
                    <Icon
                      iconName="keyboard_return"
                      $variation="600"
                      spellCheck="false"
                    />
                  }
                />
              )}
            />
            <Box
              $css={css`
                border-top: 1px solid var(--c--theme--colors--greyscale-200);
              `}
            >
              <BoxButton
                $direction="row"
                $gap="0.4rem"
                $align="center"
                $padding={{ vertical: '0.5rem', horizontal: '0.3rem' }}
                $css={css`
                  &:hover {
                    background-color: var(--c--theme--colors--greyscale-100);
                  }
                `}
                onClick={createChildDoc}
                $hasTransition={false}
              >
                <AddPageIcon />
                <Text
                  $size="14px"
                  $color="var(--c--theme--colors--greyscale-1000)"
                >
                  {t('Add a new page')}
                </Text>
              </BoxButton>
            </Box>
          </Card>
        </QuickSearch>
      </Box>
    </Box>
  );
};
