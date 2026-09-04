import {
  describe,
  expect,
  it,
} from 'vitest'

import {
  UploadParseError,
  buildUploadRequest,
  deriveUploadTitle,
  parseNotebookFile,
} from './notebookUpload'


function notebookDoc(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    nbformat: 4,
    nbformat_minor: 5,
    metadata: {},
    cells: [
      {
        id: 'cell_1',
        cell_type: 'code',
        metadata: {},
        source: 'print(1)',
        execution_count: 1,
        outputs: [
          {
            output_type: 'stream',
            name: 'stdout',
            text: '1\n',
          },
        ],
      },
    ],
    ...overrides,
  }
}


describe('parseNotebookFile', () => {
  it('合法 nbformat 4.5 文件可以解析且不改变 content', () => {
    const doc = notebookDoc()

    const parsed = parseNotebookFile(
      'demo.ipynb',
      JSON.stringify(doc),
    )

    expect(parsed.title).toBe('demo')

    expect(parsed.content).toEqual(doc)
  })

  it('nbformat 4.0～4.4、Cell 无 ID 的文件能通过客户端预检并原样保留', () => {
    const doc = notebookDoc({
      nbformat_minor: 0,
      cells: [
        {
          cell_type: 'code',
          metadata: {},
          source: 'x = 1',
          execution_count: null,
          outputs: [],
        },
      ],
    })

    const parsed = parseNotebookFile(
      'legacy.ipynb',
      JSON.stringify(doc),
    )

    expect(parsed.title).toBe('legacy')

    expect(parsed.content).toEqual(doc)
  })

  it('开头带 UTF-8 BOM 的文本可以解析', () => {
    const doc = notebookDoc()

    const parsed = parseNotebookFile(
      'bom.ipynb',
      `\uFEFF${JSON.stringify(doc)}`,
    )

    expect(parsed.content).toEqual(doc)
  })

  it('JSON 语法错误被解析函数拒绝', () => {
    expect(() =>
      parseNotebookFile(
        'broken.ipynb',
        '{ not valid json',
      ),
    ).toThrow(UploadParseError)
  })

  it('nbformat 不是 4 时在本地失败', () => {
    expect(() =>
      parseNotebookFile(
        'v3.ipynb',
        JSON.stringify(
          notebookDoc({ nbformat: 3 }),
        ),
      ),
    ).toThrow(UploadParseError)
  })

  it('缺少 metadata 时在本地失败', () => {
    const doc = notebookDoc()

    delete doc.metadata

    expect(() =>
      parseNotebookFile(
        'nometa.ipynb',
        JSON.stringify(doc),
      ),
    ).toThrow(UploadParseError)
  })

  it('metadata 是数组时在本地失败', () => {
    expect(() =>
      parseNotebookFile(
        'arraymeta.ipynb',
        JSON.stringify(
          notebookDoc({ metadata: [] }),
        ),
      ),
    ).toThrow(UploadParseError)
  })

  it('cells 项是数组时在本地失败', () => {
    expect(() =>
      parseNotebookFile(
        'arraycell.ipynb',
        JSON.stringify(
          notebookDoc({
            cells: [[{ cell_type: 'code' }]],
          }),
        ),
      ),
    ).toThrow(UploadParseError)
  })

  it('cells 不是数组时在本地失败', () => {
    expect(() =>
      parseNotebookFile(
        'badcells.ipynb',
        JSON.stringify(
          notebookDoc({ cells: 'nope' }),
        ),
      ),
    ).toThrow(UploadParseError)
  })

  it('nbformat_minor 不是不小于 0 的整数时在本地失败', () => {
    expect(() =>
      parseNotebookFile(
        'badminor.ipynb',
        JSON.stringify(
          notebookDoc({ nbformat_minor: -1 }),
        ),
      ),
    ).toThrow(UploadParseError)

    expect(() =>
      parseNotebookFile(
        'badminor.ipynb',
        JSON.stringify(
          notebookDoc({ nbformat_minor: '5' }),
        ),
      ),
    ).toThrow(UploadParseError)
  })

  it('顶层是数组时在本地失败', () => {
    expect(() =>
      parseNotebookFile(
        'array.ipynb',
        JSON.stringify([1, 2, 3]),
      ),
    ).toThrow(UploadParseError)
  })
})


describe('deriveUploadTitle', () => {
  it('取最后一个 .ipynb 之前的 basename 并 trim', () => {
    expect(
      deriveUploadTitle('  notes.ipynb  '),
    ).toBe('notes')

    expect(
      deriveUploadTitle(
        'a.ipynb.bak.ipynb',
      ),
    ).toBe('a.ipynb.bak')
  })

  it('处理后为空时返回 null（省略 title）', () => {
    expect(
      deriveUploadTitle('.ipynb'),
    ).toBeNull()
  })

  it('.ipynb 后缀匹配大小写不敏感，去后缀后再 trim', () => {
    expect(
      deriveUploadTitle('Demo.IPYNB'),
    ).toBe('Demo')

    expect(
      deriveUploadTitle('  Notes.iPyNb  '),
    ).toBe('Notes')

    expect(
      deriveUploadTitle(
        'A.ipynb.bak.IPYNB',
      ),
    ).toBe('A.ipynb.bak')

    expect(
      deriveUploadTitle('.IPYNB'),
    ).toBeNull()
  })

  it('超过 255 字符时抛错且不静默截断', () => {
    expect(() =>
      deriveUploadTitle(
        `${'x'.repeat(256)}.ipynb`,
      ),
    ).toThrow(UploadParseError)
  })
})


describe('buildUploadRequest', () => {
  it('使用文件名派生的 title 和未经改写的完整 content', () => {
    const doc = notebookDoc()

    const parsed = parseNotebookFile(
      'demo.ipynb',
      JSON.stringify(doc),
    )

    const request = buildUploadRequest(parsed)

    expect(request.title).toBe('demo')

    expect(request.content).toEqual(doc)
  })

  it('标题为空时省略 title 字段', () => {
    const doc = notebookDoc()

    const parsed = parseNotebookFile(
      '.ipynb',
      JSON.stringify(doc),
    )

    const request = buildUploadRequest(parsed)

    expect('title' in request).toBe(false)
    expect(request.content).toEqual(doc)
  })
})
