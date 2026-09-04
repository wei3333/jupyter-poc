import type * as nbformat from '@jupyterlab/nbformat'


/*
 * 浏览器内部文档状态。
 *
 * dirty 不再作为独立字段保存，而是由
 *
 *   dirty = generation !== savedGeneration
 *
 * 推导：每次本地文档变化只增加一次 generation；
 * 服务端响应不会覆盖较新的本地 content。
 */
export interface NotebookDocumentState {
  notebookId: string
  title: string
  revision: number
  contentHash: string
  createdAt: string
  updatedAt: string
  etag: string | null
  content: nbformat.INotebookContent
  generation: number
  savedGeneration: number
}


/*
 * Notebook Service v1 HTTP envelope。
 * content 引用官方 @jupyterlab/nbformat 类型。
 */
export interface CreateNotebookRequest {
  title?: string
  content?: nbformat.INotebookContent
}

export interface SaveNotebookRequest {
  baseRevision: number
  content: nbformat.INotebookContent
}

export interface NotebookDocumentResponse {
  notebookId: string
  title: string
  revision: number
  contentHash: string
  createdAt: string
  updatedAt: string
  content: nbformat.INotebookContent
}

export interface SaveNotebookResponse {
  notebookId: string
  revision: number
  contentHash: string
  updatedAt: string
  unchanged: boolean
}


/*
 * 列表摘要。列表接口不返回 content/contentHash，
 * 客户端不得为列表项假设这些字段。
 */
export interface NotebookSummary {
  notebookId: string
  title: string
  revision: number
  createdAt: string
  updatedAt: string
}

export interface NotebookListResponse {
  items: NotebookSummary[]
  nextCursor: string | null
}


/*
 * 幂等写操作：一次逻辑写操作对应一个 key + 一份只序列化一次的 body。
 * 因网络错误或 5xx/503 重试时必须复用同一个 IdempotentOperation；
 * 请求内容改变时创建新操作和新 key。
 */
export interface IdempotentOperation {
  idempotencyKey: string
  body: string
}


/*
 * 服务端统一错误 envelope 的错误码。
 * NETWORK_ERROR / UNKNOWN 是客户端在无法得到服务端 envelope
 * （网络失败、非 JSON 错误体）时使用的本地码。
 */
export type NotebookApiErrorCode =
  | 'MALFORMED_JSON'
  | 'INVALID_REQUEST'
  | 'INVALID_CURSOR'
  | 'INVALID_NOTEBOOK'
  | 'NOTEBOOK_NOT_FOUND'
  | 'REVISION_NOT_FOUND'
  | 'REVISION_CONFLICT'
  | 'IDEMPOTENCY_KEY_REUSED'
  | 'PAYLOAD_TOO_LARGE'
  | 'INTERNAL_ERROR'
  | 'STORAGE_UNAVAILABLE'
  | 'UNAUTHENTICATED'
  | 'PERMISSION_DENIED'
  | 'QUOTA_EXCEEDED'
  | 'NETWORK_ERROR'
  | 'UNKNOWN'

export class NotebookApiError extends Error {
  readonly status: number
  readonly code: NotebookApiErrorCode
  readonly requestId: string | null
  readonly details: unknown
  readonly retryAfter: number | null

  constructor(
    status: number,
    code: NotebookApiErrorCode,
    message: string,
    requestId: string | null,
    details: unknown,
    retryAfter: number | null,
  ) {
    super(message)

    this.name = 'NotebookApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.details = details
    this.retryAfter = retryAfter
  }
}
