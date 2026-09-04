import type * as nbformat from '@jupyterlab/nbformat'

import type {
  CreateNotebookRequest,
} from './types'


/*
 * 本地文件读取 / JSON 解析 / 便宜顶层预检失败
 * 属于上传 UI 错误，不伪装成 NotebookApiError
 * 或 NETWORK_ERROR；出错后不得发送 POST。
 */
export class UploadParseError extends Error {
  constructor(message: string) {
    super(message)

    this.name = 'UploadParseError'
  }
}


export interface ParsedNotebookFile {
  /*
   * 派生标题。null 表示省略 title，
   * 使用服务端默认标题。
   */
  title: string | null

  /*
   * 未经改写的完整解析结果。
   * 客户端只做顶层预检，Cell/outputs/metadata
   * 的完整校验由 Notebook Service 负责。
   */
  content: unknown
}


/*
 * 取文件名最后一个 .ipynb 之前的 basename：
 * 后缀匹配大小写不敏感（.ipynb / .IPYNB / .iPyNb），
 * 去后缀后再 trim。
 * 非空且不超过 255 字符时作为 title；
 * 为空返回 null（省略 title）；
 * 超过 255 字符抛 UploadParseError（不静默截断）。
 */
export function deriveUploadTitle(
  fileName: string,
): string | null {
  const basename = (
    fileName.split('/').pop() ?? fileName
  ).trim()

  const withoutExt =
    basename.toLowerCase().endsWith('.ipynb')
      ? basename.slice(0, -'.ipynb'.length)
      : basename

  const title = withoutExt.trim()

  if (title === '') {
    return null
  }

  if (title.length > 255) {
    throw new UploadParseError(
      '文件名过长：标题超过 255 字符上限，请改名后再上传',
    )
  }

  return title
}


function stripBom(text: string): string {
  return (
    text.charCodeAt(0) === 0xfeff
      ? text.slice(1)
      : text
  )
}


/*
 * 解析本地 .ipynb 文本并做便宜顶层预检：
 *
 * - UTF-8 JSON 文本，允许开头 BOM；
 * - 结果是非数组对象；
 * - nbformat === 4；
 * - nbformat_minor 是 >= 0 的整数；
 * - metadata 是对象（明确拒绝数组）；
 * - cells 是数组且 Cell 项是对象（明确拒绝数组）。
 *
 * 不要求 nbformat_minor >= 5，也不要求每个 Cell
 * 已有 ID：创建/导入契约兼容 nbformat 4.0～4.4，
 * Notebook Service 会提升 minor version 并补齐
 * 缺失、重复或非法的 Cell ID。
 * Portal 不修改上传文件的任何 Cell、source、
 * outputs、metadata 或 attachments。
 */
export function parseNotebookFile(
  fileName: string,
  text: string,
): ParsedNotebookFile {
  let parsed: unknown

  try {
    parsed = JSON.parse(stripBom(text))
  } catch {
    throw new UploadParseError(
      '文件不是合法的 JSON，无法解析为 Notebook',
    )
  }

  if (
    typeof parsed !== 'object'
    || parsed === null
    || Array.isArray(parsed)
  ) {
    throw new UploadParseError(
      '文件内容不是 Notebook 对象',
    )
  }

  const doc = parsed as Record<string, unknown>

  if (doc.nbformat !== 4) {
    throw new UploadParseError(
      '不支持的 Notebook 格式：仅支持 nbformat 4',
    )
  }

  if (
    typeof doc.nbformat_minor !== 'number'
    || !Number.isInteger(doc.nbformat_minor)
    || doc.nbformat_minor < 0
  ) {
    throw new UploadParseError(
      'nbformat_minor 必须是不小于 0 的整数',
    )
  }

  if (
    typeof doc.metadata !== 'object'
    || doc.metadata === null
    || Array.isArray(doc.metadata)
  ) {
    throw new UploadParseError(
      'Notebook 缺少 metadata',
    )
  }

  if (
    !Array.isArray(doc.cells)
    || doc.cells.some(
      (cell) => (
        typeof cell !== 'object'
        || cell === null
        || Array.isArray(cell)
      ),
    )
  ) {
    throw new UploadParseError(
      'Notebook 的 cells 必须是 Cell 对象数组',
    )
  }

  return {
    title: deriveUploadTitle(fileName),
    content: parsed,
  }
}


/*
 * 由解析结果构造 POST body：
 * content 原样使用解析结果（不做任何改写），
 * title 在非空时带上文件名派生的标题。
 */
export function buildUploadRequest(
  parsed: ParsedNotebookFile,
): CreateNotebookRequest {
  const request: CreateNotebookRequest = {
    content:
      parsed.content as nbformat.INotebookContent,
  }

  if (parsed.title !== null) {
    request.title = parsed.title
  }

  return request
}
