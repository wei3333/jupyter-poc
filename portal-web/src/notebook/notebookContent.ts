import type * as nbformat from '@jupyterlab/nbformat'


/*
 * 产品侧最小创建模板：nbformat 4.5 + 一个空 Code Cell。
 * Cell ID 使用去掉连字符的 crypto.randomUUID()，
 * 满足 [A-Za-z0-9_-]{1,64} 且每次构造唯一。
 * 模板属于 Portal，不写入 Notebook Service。
 */
export function buildBlankNotebook(
): nbformat.INotebookContent {
  const cell: nbformat.ICodeCell = {
    id: crypto.randomUUID()
      .replace(/-/g, ''),
    cell_type: 'code',
    metadata: {},
    source: '',
    execution_count: null,
    outputs: [],
  }

  return {
    nbformat: 4,
    nbformat_minor: 5,
    metadata: {},
    cells: [cell],
  }
}
