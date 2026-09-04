import {
  describe,
  expect,
  it,
} from 'vitest'

import { isCode } from '@jupyterlab/nbformat'

import {
  buildBlankNotebook,
} from './notebookContent'


describe('buildBlankNotebook', () => {
  it('生成 nbformat 4.5 文档', () => {
    const doc = buildBlankNotebook()

    expect(doc.nbformat).toBe(4)
    expect(doc.nbformat_minor).toBe(5)
    expect(doc.metadata).toEqual({})
  })

  it('恰好包含一个空 Code Cell', () => {
    const doc = buildBlankNotebook()

    expect(doc.cells).toHaveLength(1)

    const cell = doc.cells[0]

    if (!isCode(cell)) {
      throw new Error(
        'expected a code cell',
      )
    }

    expect(cell.cell_type).toBe('code')
    expect(cell.source).toBe('')
    expect(cell.metadata).toEqual({})
  })

  it('Code Cell 的 execution_count 为 null 且 outputs 为空', () => {
    const doc = buildBlankNotebook()

    const cell = doc.cells[0]

    if (!isCode(cell)) {
      throw new Error(
        'expected a code cell',
      )
    }

    expect(cell.execution_count).toBeNull()
    expect(cell.outputs).toEqual([])
  })

  it('Cell ID 合法；连续构造两份模板时 ID 不同', () => {
    const first = buildBlankNotebook()
    const second = buildBlankNotebook()

    const firstCell = first.cells[0]
    const secondCell = second.cells[0]

    if (!isCode(firstCell) || !isCode(secondCell)) {
      throw new Error(
        'expected code cells',
      )
    }

    expect(firstCell.id).toMatch(
      /^[A-Za-z0-9_-]{1,64}$/,
    )

    expect(secondCell.id).toMatch(
      /^[A-Za-z0-9_-]{1,64}$/,
    )

    expect(firstCell.id)
      .not.toBe(secondCell.id)
  })
})
