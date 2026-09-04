import {
  NotebookApiError,
  type IdempotentOperation,
  type NotebookApiErrorCode,
  type NotebookDocumentResponse,
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
