import {
  describe,
  expect,
  it,
} from 'vitest'

import {
  filterOutNotebook,
} from './useNotebookList'

import type {
  NotebookSummary,
} from './types'


function summary(
  notebookId: string,
): NotebookSummary {
  return {
    notebookId,
    title: notebookId,
    revision: 1,
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z',
  }
}


describe('filterOutNotebook', () => {
  it('只删除目标 ID，保持其余项顺序', () => {
    const items = [
      summary('nb_a'),
      summary('nb_b'),
      summary('nb_c'),
    ]

    const result = filterOutNotebook(
      items,
      'nb_b',
    )

    expect(result).toEqual([
      summary('nb_a'),
      summary('nb_c'),
    ])
  })

  it('目标 ID 不存在时返回原顺序的副本', () => {
    const items = [
      summary('nb_a'),
      summary('nb_b'),
    ]

    const result = filterOutNotebook(
      items,
      'nb_missing',
    )

    expect(result).toEqual(items)
    expect(result).not.toBe(items)
  })

  it('删除唯一一项后得到空数组', () => {
    const result = filterOutNotebook(
      [summary('nb_only')],
      'nb_only',
    )

    expect(result).toEqual([])
  })
})
