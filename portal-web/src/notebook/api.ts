import {
  NotebookApiError,
  type IdempotentOperation,
  type NotebookApiErrorCode,
  type NotebookDocumentResponse,
  type NotebookListResponse,
  type SaveNotebookResponse,
} from './types'


const NOTEBOOKS_API = '/api/v1/notebooks'


/*
 * GET 结果。304 时不能调用 response.json()，
 * 返回明确的 not-modified 结果，由调用方继续使用已有文档。
 */
export type GetNotebookResult =
  | {
      kind: 'ok'
      document: NotebookDocumentResponse
      etag: string | null
      requestId: string | null
    }
  | {
      kind: 'not-modified'
      etag: string | null
      requestId: string | null
    }

export interface CreateNotebookResult {
  document: NotebookDocumentResponse
  etag: string | null
  location: string | null
  requestId: string | null
}

export interface SaveNotebookResult
  extends SaveNotebookResponse {
  etag: string | null
  requestId: string | null
}


export interface ListNotebooksOptions {
  limit?: number
  cursor?: string
  signal?: AbortSignal
}

export interface ListNotebooksResult
  extends NotebookListResponse {
  requestId: string | null
}


/*
 * 主动中止（AbortSignal）的请求是调用方控制流，
 * 不转换为错误；由调用方自行忽略。
 */
export function isAbortError(
  error: unknown,
): boolean {
  return (
    error instanceof DOMException
    && error.name === 'AbortError'
  )
}


/*
 * 为一次逻辑写操作创建幂等 key + 只序列化一次的 body。
 * 调用方负责在未知结果重试时复用同一个 IdempotentOperation。
 */
export function createIdempotentOperation(
  requestBody: unknown,
): IdempotentOperation {
  return {
    idempotencyKey: crypto.randomUUID(),
    body: JSON.stringify(requestBody),
  }
}


function requestIdOf(
  response: Response,
): string | null {
  return response.headers.get('X-Request-ID')
}


function retryAfterOf(
  response: Response,
): number | null {
  const raw = response.headers.get('Retry-After')

  if (raw === null) {
    return null
  }

  const seconds = Number.parseInt(raw, 10)

  if (
    !Number.isInteger(seconds)
    || seconds < 0
  ) {
    return null
  }

  return seconds
}


function toNetworkError(
  error: unknown,
): NotebookApiError {
  return new NotebookApiError(
    0,
    'NETWORK_ERROR',
    error instanceof Error
      ? error.message
      : String(error),
    null,
    null,
    null,
  )
}


/*
 * 把任意错误转成 NotebookApiError。
 * 无法解析统一错误 envelope 时仍保留 HTTP status
 * 和响应头中的 Request ID，不向 UI 抛裸字符串。
 */
export function asNotebookApiError(
  error: unknown,
): NotebookApiError {
  if (error instanceof NotebookApiError) {
    return error
  }

  return new NotebookApiError(
    0,
    'UNKNOWN',
    error instanceof Error
      ? error.message
      : String(error),
    null,
    null,
    null,
  )
}


async function parseErrorResponse(
  response: Response,
): Promise<NotebookApiError> {
  const status = response.status
  const requestId = requestIdOf(response)
  const retryAfter = retryAfterOf(response)

  let code: NotebookApiErrorCode = 'UNKNOWN'
  let message =
    `Request failed with status ${status}`
  let details: unknown = null

  try {
    const body: unknown = await response.json()

    if (
      typeof body === 'object'
      && body !== null
      && 'error' in body
    ) {
      const errorBody = (
        body as { error: unknown }
      ).error

      if (
        typeof errorBody === 'object'
        && errorBody !== null
      ) {
        const error = errorBody as {
          code?: unknown
          message?: unknown
          details?: unknown
        }

        if (typeof error.code === 'string') {
          code = error.code as NotebookApiErrorCode
        }

        if (typeof error.message === 'string') {
          message = error.message
        }

        if ('details' in error) {
          details = error.details
        }
      }
    }
  } catch {
    /*
     * 响应体不是统一错误 envelope：
     * 保留 HTTP status 与响应头 Request ID。
     */
  }

  return new NotebookApiError(
    status,
    code,
    message,
    requestId,
    details,
    retryAfter,
  )
}


/*
 * GET /api/v1/notebooks
 *
 * - limit 只允许 1～100，页面固定使用 20；
 * - cursor 是不透明字符串，只允许原样回传，
 *   禁止解析、拼接或自行构造；
 * - 列表没有 collection ETag，不发送 If-None-Match；
 * - 空列表是正常的 200。
 */
export async function listNotebooks(
  options: ListNotebooksOptions = {},
): Promise<ListNotebooksResult> {
  const limit = Math.min(
    100,
    Math.max(1, options.limit ?? 20),
  )

  const params = new URLSearchParams()

  params.set('limit', String(limit))

  if (options.cursor) {
    params.set('cursor', options.cursor)
  }

  let response: Response

  try {
    response = await fetch(
      `${NOTEBOOKS_API}?${params.toString()}`,
      options.signal
        ? { signal: options.signal }
        : undefined,
    )
  } catch (error) {
    if (isAbortError(error)) {
      throw error
    }

    throw toNetworkError(error)
  }

  if (!response.ok) {
    throw await parseErrorResponse(response)
  }

  const body =
    await response.json() as NotebookListResponse

  return {
    ...body,
    requestId: requestIdOf(response),
  }
}


export async function createNotebook(
  operation: IdempotentOperation,
): Promise<CreateNotebookResult> {
  let response: Response

  try {
    response = await fetch(
      NOTEBOOKS_API,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key':
            operation.idempotencyKey,
        },
        body: operation.body,
      },
    )
  } catch (error) {
    throw toNetworkError(error)
  }

  if (!response.ok) {
    throw await parseErrorResponse(response)
  }

  return {
    document:
      await response.json() as NotebookDocumentResponse,
    etag: response.headers.get('ETag'),
    location:
      response.headers.get('Location'),
    requestId: requestIdOf(response),
  }
}


export async function getNotebook(
  notebookId: string,
  options: {
    etag?: string | null
  } = {},
): Promise<GetNotebookResult> {
  return getNotebookAtPath(
    `${NOTEBOOKS_API}/${encodeURIComponent(notebookId)}`,
    options,
  )
}


export async function getNotebookRevision(
  notebookId: string,
  revision: number,
  options: {
    etag?: string | null
  } = {},
): Promise<GetNotebookResult> {
  return getNotebookAtPath(
    `${NOTEBOOKS_API}/${encodeURIComponent(notebookId)}/revisions/${revision}`,
    options,
  )
}


async function getNotebookAtPath(
  path: string,
  options: {
    etag?: string | null
  },
): Promise<GetNotebookResult> {
  const headers: Record<string, string> = {}

  if (options.etag) {
    headers['If-None-Match'] = options.etag
  }

  let response: Response

  try {
    response = await fetch(path, { headers })
  } catch (error) {
    throw toNetworkError(error)
  }

  const etag = response.headers.get('ETag')
  const requestId = requestIdOf(response)

  if (response.status === 304) {
    return {
      kind: 'not-modified',
      etag,
      requestId,
    }
  }

  if (!response.ok) {
    throw await parseErrorResponse(response)
  }

  return {
    kind: 'ok',
    document:
      await response.json() as NotebookDocumentResponse,
    etag,
    requestId,
  }
}


export async function saveNotebook(
  notebookId: string,
  operation: IdempotentOperation,
): Promise<SaveNotebookResult> {
  let response: Response

  try {
    response = await fetch(
      `${NOTEBOOKS_API}/${encodeURIComponent(notebookId)}`,
      {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'Idempotency-Key':
            operation.idempotencyKey,
        },
        body: operation.body,
      },
    )
  } catch (error) {
    throw toNetworkError(error)
  }

  if (!response.ok) {
    throw await parseErrorResponse(response)
  }

  const body =
    await response.json() as SaveNotebookResponse

  return {
    ...body,
    etag: response.headers.get('ETag'),
    requestId: requestIdOf(response),
  }
}


export interface DeleteNotebookOptions {
  signal?: AbortSignal
}

export interface DeleteNotebookResult {
  requestId: string | null
}


/*
 * DELETE /api/v1/notebooks/{notebookId}
 *
 * - 不发送 body、Content-Type、Idempotency-Key、
 *   baseRevision 或 If-Match；
 * - 204 无响应体，绝不调用 response.json()；
 * - 首次删除与重复删除均返回 204，客户端
 *   无需 pending operation；
 * - 契约之外的意外 2xx 转换为可诊断错误，
 *   不静默接受，避免隐藏契约漂移。
 */
export async function deleteNotebook(
  notebookId: string,
  options: DeleteNotebookOptions = {},
): Promise<DeleteNotebookResult> {
  const init: RequestInit = {
    method: 'DELETE',
  }

  if (options.signal) {
    init.signal = options.signal
  }

  let response: Response

  try {
    response = await fetch(
      `${NOTEBOOKS_API}/${encodeURIComponent(notebookId)}`,
      init,
    )
  } catch (error) {
    if (isAbortError(error)) {
      throw error
    }

    throw toNetworkError(error)
  }

  if (response.status === 204) {
    return {
      requestId: requestIdOf(response),
    }
  }

  if (response.ok) {
    throw new NotebookApiError(
      response.status,
      'UNKNOWN',
      `DELETE 返回了契约之外的状态码 ${response.status}，请检查 Notebook Service 版本`,
      requestIdOf(response),
      null,
      null,
    )
  }

  throw await parseErrorResponse(response)
}
