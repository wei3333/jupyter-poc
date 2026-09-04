import {
  afterEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

import type * as nbformat from '@jupyterlab/nbformat'

import {
  createIdempotentOperation,
  createNotebook,
  deleteNotebook,
  getNotebook,
  getNotebookRevision,
  listNotebooks,
  saveNotebook,
} from './api'

import {
  NotebookApiError,
  type CreateNotebookRequest,
  type NotebookDocumentResponse,
  type SaveNotebookRequest,
} from './types'


function emptyNotebook(
): nbformat.INotebookContent {
  return {
    nbformat: 4,
    nbformat_minor: 5,
    metadata: {},
    cells: [],
  }
}


function documentResponse(
  overrides:
    Partial<NotebookDocumentResponse> = {},
): NotebookDocumentResponse {
  return {
    notebookId: 'nb_abc123',
    title: 'Untitled Notebook',
    revision: 1,
    contentHash:
      'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z',
    content: emptyNotebook(),
    ...overrides,
  }
}


function jsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(
    JSON.stringify(body),
    { status, headers },
  )
}


interface FetchCall {
  url: string
  init: RequestInit | undefined
}


function installFetchMock(
  responder: (
    call: FetchCall,
  ) => Response | Promise<Response>,
) {
  const fetchMock = vi.fn(
    async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      return responder({
        url: String(input),
        init,
      })
    },
  )

  vi.stubGlobal('fetch', fetchMock)

  return fetchMock
}


function headersOf(
  fetchMock: ReturnType<typeof vi.fn>,
  callIndex: number,
): Record<string, string> {
  const call = fetchMock.mock.calls[callIndex]

  const init =
    call?.[1] as RequestInit | undefined

  return (
    init?.headers as
      Record<string, string> | undefined
  ) ?? {}
}


afterEach(() => {
  vi.unstubAllGlobals()
})


describe('listNotebooks', () => {
  it('首次列表请求使用 GET /api/v1/notebooks?limit=20，不带 cursor', async () => {
    const fetchMock = installFetchMock(
      ({ url, init }) => {
        expect(url).toBe(
          '/api/v1/notebooks?limit=20',
        )

        expect(init?.method).toBeUndefined()

        return jsonResponse(
          {
            items: [],
            nextCursor: null,
          },
          200,
          {
            'X-Request-ID': 'req_list_1',
          },
        )
      },
    )

    const result = await listNotebooks({
      limit: 20,
    })

    /*
     * 空列表是正常的成功结果。
     */
    expect(result.items).toEqual([])
    expect(result.nextCursor).toBeNull()
    expect(result.requestId)
      .toBe('req_list_1')

    /*
     * 列表没有 collection ETag，
     * 不发送 If-None-Match。
     */
    expect(
      headersOf(fetchMock, 0)['If-None-Match'],
    ).toBeUndefined()
  })

  it('cursor 通过 URLSearchParams 正确编码并原样传递', async () => {
    const cursor = 'abc/def+%'

    installFetchMock(({ url }) => {
      expect(url).toBe(
        '/api/v1/notebooks?limit=20&cursor=abc%2Fdef%2B%25',
      )

      return jsonResponse({
        items: [],
        nextCursor: null,
      })
    })

    await listNotebooks({
      limit: 20,
      cursor,
    })
  })

  it('正确解析 items、nextCursor 和 X-Request-ID', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          items: [
            {
              notebookId: 'nb_item_1',
              title: 'First',
              revision: 3,
              createdAt: '2026-09-04T00:00:00Z',
              updatedAt: '2026-09-04T01:00:00Z',
            },
            {
              notebookId: 'nb_item_2',
              title: 'Second',
              revision: 1,
              createdAt: '2026-09-03T00:00:00Z',
              updatedAt: '2026-09-03T01:00:00Z',
            },
          ],
          nextCursor: 'opaque_cursor_1',
        },
        200,
        {
          'X-Request-ID': 'req_list_2',
        },
      ),
    )

    const result = await listNotebooks({
      limit: 20,
    })

    expect(result.items).toHaveLength(2)

    expect(result.items[0].notebookId)
      .toBe('nb_item_1')

    expect(result.items[0].revision).toBe(3)

    expect(result.items[1].updatedAt)
      .toBe('2026-09-03T01:00:00Z')

    expect(result.nextCursor)
      .toBe('opaque_cursor_1')

    expect(result.requestId)
      .toBe('req_list_2')
  })

  it('400 INVALID_CURSOR 被解析为 NotebookApiError', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'INVALID_CURSOR',
            message: 'Pagination cursor is invalid',
            requestId: 'req_cursor_1',
          },
        },
        400,
        {
          'X-Request-ID': 'req_cursor_1',
        },
      ),
    )

    const error = await listNotebooks({
      cursor: 'stale_cursor',
    }).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(400)
    expect(apiError.code)
      .toBe('INVALID_CURSOR')

    expect(apiError.requestId)
      .toBe('req_cursor_1')
  })

  it('网络失败仍得到 NETWORK_ERROR', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )

    const error = await listNotebooks({
      limit: 20,
    }).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(0)
    expect(apiError.code).toBe('NETWORK_ERROR')
  })

  it('主动中止的请求原样抛出 AbortError 而不伪装成网络错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (
          _input: RequestInfo | URL,
          init?: RequestInit,
        ) => (
          new Promise<Response>(
            (_resolve, reject) => {
              init?.signal?.addEventListener(
                'abort',
                () => {
                  reject(
                    new DOMException(
                      'The operation was aborted',
                      'AbortError',
                    ),
                  )
                },
              )
            },
          )
        ),
      ),
    )

    const controller = new AbortController()

    const pending = listNotebooks({
      limit: 20,
      signal: controller.signal,
    })

    controller.abort()

    const error = await pending.then(
      () => null,
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(DOMException)

    expect((error as DOMException).name)
      .toBe('AbortError')
  })
})


describe('createNotebook', () => {
  it('POST /api/v1/notebooks 并携带 Idempotency-Key 与预序列化 body', async () => {
    const request: CreateNotebookRequest = {
      title: 'Hello',
      content: emptyNotebook(),
    }

    const operation =
      createIdempotentOperation(request)

    const fetchMock = installFetchMock(
      ({ url, init }) => {
        expect(url)
          .toBe('/api/v1/notebooks')

        expect(init?.method).toBe('POST')

        expect(init?.body)
          .toBe(operation.body)

        return jsonResponse(
          documentResponse({
            notebookId: 'nb_new01',
            revision: 1,
          }),
          201,
          {
            ETag: '"nb_new01-r1"',
            Location:
              '/api/v1/notebooks/nb_new01',
            'X-Request-ID': 'req_create_1',
          },
        )
      },
    )

    const result = await createNotebook(
      operation,
    )

    expect(
      headersOf(fetchMock, 0)['Idempotency-Key'],
    ).toBe(operation.idempotencyKey)

    expect(result.document.notebookId)
      .toBe('nb_new01')

    expect(result.etag).toBe('"nb_new01-r1"')

    expect(result.location)
      .toBe('/api/v1/notebooks/nb_new01')

    expect(result.requestId)
      .toBe('req_create_1')
  })

  it('重复调用使用同一个 op 时发送相同 key 和相同 body', async () => {
    const request: CreateNotebookRequest = {
      content: emptyNotebook(),
    }

    const operation =
      createIdempotentOperation(request)

    const fetchMock = installFetchMock(() =>
      jsonResponse(
        documentResponse(),
        201,
        { 'X-Request-ID': 'req_1' },
      ),
    )

    await createNotebook(operation)

    await createNotebook(operation)

    expect(fetchMock).toHaveBeenCalledTimes(2)

    expect(
      headersOf(fetchMock, 0)['Idempotency-Key'],
    ).toBe(
      headersOf(fetchMock, 1)['Idempotency-Key'],
    )

    expect(fetchMock.mock.calls[0][1]?.body)
      .toBe(fetchMock.mock.calls[1][1]?.body)
  })
})


describe('getNotebook', () => {
  it('解析统一错误 envelope 为 NotebookApiError', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'NOTEBOOK_NOT_FOUND',
            message: 'Notebook was not found',
            requestId: 'req_missing_1',
            details: {
              notebookId: 'nb_missing',
            },
          },
        },
        404,
        {
          'X-Request-ID': 'req_missing_1',
        },
      ),
    )

    const error = await getNotebook(
      'nb_missing',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(404)
    expect(apiError.code)
      .toBe('NOTEBOOK_NOT_FOUND')

    expect(apiError.requestId)
      .toBe('req_missing_1')

    expect(apiError.details)
      .toEqual({
        notebookId: 'nb_missing',
      })
  })

  it('提供 etag 时发送 If-None-Match，不提供时不发送', async () => {
    const fetchMock = installFetchMock(() =>
      jsonResponse(
        documentResponse(),
        200,
        { ETag: '"etag-1"' },
      ),
    )

    await getNotebook('nb_abc123')

    await getNotebook('nb_abc123', {
      etag: '"etag-1"',
    })

    const first = headersOf(fetchMock, 0)
    const second = headersOf(fetchMock, 1)

    expect(first['If-None-Match'])
      .toBeUndefined()

    expect(second['If-None-Match'])
      .toBe('"etag-1"')
  })

  it('304 返回 not-modified 且不解析 JSON body', async () => {
    installFetchMock(() =>
      new Response(null, {
        status: 304,
        headers: {
          ETag: '"etag-1"',
          'X-Request-ID': 'req_304',
        },
      }),
    )

    /*
     * 304 响应没有 JSON body：
     * 如果实现调用了 response.json()，
     * 这里会抛出解析错误而不是返回结果。
     */
    const result = await getNotebook(
      'nb_abc123',
      { etag: '"etag-1"' },
    )

    expect(result.kind).toBe('not-modified')

    expect(result.etag).toBe('"etag-1"')

    expect(result.requestId).toBe('req_304')
  })

  it('错误响应体不是统一 envelope 时仍转换为 NotebookApiError', async () => {
    installFetchMock(() =>
      new Response(
        '<html>Bad Gateway</html>',
        {
          status: 502,
          headers: {
            'X-Request-ID': 'req_gateway_1',
          },
        },
      ),
    )

    const error = await getNotebook(
      'nb_abc123',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(502)
    expect(apiError.code).toBe('UNKNOWN')
    expect(apiError.requestId)
      .toBe('req_gateway_1')
  })

  it('网络失败转换为 NETWORK_ERROR，不抛裸字符串', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )

    const error = await getNotebook(
      'nb_abc123',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(0)
    expect(apiError.code).toBe('NETWORK_ERROR')
  })

  it('503 的 Retry-After 被解析进 NotebookApiError', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'STORAGE_UNAVAILABLE',
            message:
              'Notebook storage is temporarily unavailable',
            requestId: 'req_storage_1',
          },
        },
        503,
        {
          'Retry-After': '5',
          'X-Request-ID': 'req_storage_1',
        },
      ),
    )

    const error = await getNotebook(
      'nb_abc123',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(503)
    expect(apiError.code)
      .toBe('STORAGE_UNAVAILABLE')

    expect(apiError.retryAfter).toBe(5)
  })
})


describe('getNotebookRevision', () => {
  it('读取 /revisions/{revision} 路径', async () => {
    installFetchMock(
      ({ url }) => {
        expect(url).toBe(
          '/api/v1/notebooks/nb_abc123/revisions/3',
        )

        return jsonResponse(
          documentResponse({ revision: 3 }),
          200,
          { ETag: '"nb_abc123-r3"' },
        )
      },
    )

    const result = await getNotebookRevision(
      'nb_abc123',
      3,
    )

    expect(result.kind).toBe('ok')

    if (result.kind === 'ok') {
      expect(result.document.revision).toBe(3)
      expect(result.etag)
        .toBe('"nb_abc123-r3"')
    }
  })
})


describe('saveNotebook', () => {
  it('PUT 携带 Idempotency-Key，retry 复用同一 key 和 body', async () => {
    const request: SaveNotebookRequest = {
      baseRevision: 1,
      content: emptyNotebook(),
    }

    const operation =
      createIdempotentOperation(request)

    const fetchMock = installFetchMock(
      ({ url, init }) => {
        expect(url).toBe(
          '/api/v1/notebooks/nb_abc123',
        )

        expect(init?.method).toBe('PUT')

        expect(init?.body)
          .toBe(operation.body)

        return jsonResponse(
          {
            notebookId: 'nb_abc123',
            revision: 2,
            contentHash:
              'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
            updatedAt: '2026-09-04T01:00:00Z',
            unchanged: false,
          },
          200,
          {
            ETag: '"nb_abc123-r2"',
            'X-Request-ID': 'req_save_1',
          },
        )
      },
    )

    const first = await saveNotebook(
      'nb_abc123',
      operation,
    )

    const second = await saveNotebook(
      'nb_abc123',
      operation,
    )

    expect(
      headersOf(fetchMock, 0)['Idempotency-Key'],
    ).toBe(operation.idempotencyKey)

    expect(
      headersOf(fetchMock, 0)['Idempotency-Key'],
    ).toBe(
      headersOf(fetchMock, 1)['Idempotency-Key'],
    )

    expect(fetchMock.mock.calls[0][1]?.body)
      .toBe(fetchMock.mock.calls[1][1]?.body)

    expect(first.revision).toBe(2)
    expect(first.etag).toBe('"nb_abc123-r2"')
    expect(first.requestId).toBe('req_save_1')

    expect(second.revision).toBe(2)
  })

  it('409 REVISION_CONFLICT 解析出错误码和 details', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'REVISION_CONFLICT',
            message: 'Notebook has been modified',
            requestId: 'req_conflict_1',
            details: {
              baseRevision: 1,
              currentRevision: 3,
            },
          },
        },
        409,
        {
          'X-Request-ID': 'req_conflict_1',
        },
      ),
    )

    const error = await saveNotebook(
      'nb_abc123',
      createIdempotentOperation({
        baseRevision: 1,
        content: emptyNotebook(),
      }),
    ).then(
      () => null,
      (e: unknown) => e,
    )

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(409)
    expect(apiError.code)
      .toBe('REVISION_CONFLICT')

    expect(apiError.details)
      .toEqual({
        baseRevision: 1,
        currentRevision: 3,
      })
  })
})


describe('deleteNotebook', () => {
  it('DELETE 使用编码后的 notebookId 路径和 method: DELETE', async () => {
    const fetchMock = installFetchMock(
      ({ url, init }) => {
        expect(url).toBe(
          '/api/v1/notebooks/nb_abc123',
        )

        expect(init?.method).toBe('DELETE')

        return new Response(null, {
          status: 204,
          headers: {
            'X-Request-ID': 'req_del_1',
          },
        })
      },
    )

    const result = await deleteNotebook(
      'nb_abc123',
    )

    expect(fetchMock).toHaveBeenCalledTimes(1)

    expect(result.requestId).toBe('req_del_1')
  })

  it('请求没有 body、Content-Type、Idempotency-Key 和 If-Match', async () => {
    const fetchMock = installFetchMock(
      ({ init }) => {
        expect(init?.body).toBeUndefined()

        return new Response(null, {
          status: 204,
        })
      },
    )

    await deleteNotebook('nb_abc123')

    const headers = headersOf(fetchMock, 0)

    expect(headers['Content-Type'])
      .toBeUndefined()

    expect(headers['Idempotency-Key'])
      .toBeUndefined()

    expect(headers['If-Match'])
      .toBeUndefined()
  })

  it('204 返回 requestId 且绝不调用 response.json()', async () => {
    /*
     * 204 没有响应体：如果实现调用了
     * response.json()，这里会抛出解析错误
     * 而不是返回结果。
     */
    installFetchMock(() =>
      new Response(null, {
        status: 204,
        headers: {
          'X-Request-ID': 'req_del_2',
        },
      }),
    )

    const result = await deleteNotebook(
      'nb_abc123',
    )

    expect(result.requestId).toBe('req_del_2')
  })

  it('连续两次 DELETE 都收到 204 时均按成功处理', async () => {
    const fetchMock = installFetchMock(() =>
      new Response(null, { status: 204 }),
    )

    const first = await deleteNotebook(
      'nb_abc123',
    )

    const second = await deleteNotebook(
      'nb_abc123',
    )

    expect(first.requestId).toBeNull()
    expect(second.requestId).toBeNull()

    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('404 NOTEBOOK_NOT_FOUND 解析为结构化 NotebookApiError', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'NOTEBOOK_NOT_FOUND',
            message: 'Notebook was not found',
            requestId: 'req_del_404',
            details: {
              notebookId: 'nb_missing',
            },
          },
        },
        404,
        {
          'X-Request-ID': 'req_del_404',
        },
      ),
    )

    const error = await deleteNotebook(
      'nb_missing',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(404)
    expect(apiError.code)
      .toBe('NOTEBOOK_NOT_FOUND')

    expect(apiError.requestId)
      .toBe('req_del_404')
  })

  it('422 INVALID_REQUEST 解析为结构化 NotebookApiError', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'INVALID_REQUEST',
            message:
              'Request does not match the API contract',
            requestId: 'req_del_422',
            details: {
              path: '/notebookId',
              reason: 'String should match pattern',
            },
          },
        },
        422,
        {
          'X-Request-ID': 'req_del_422',
        },
      ),
    )

    const error = await deleteNotebook(
      'nb_bad!id',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(422)
    expect(apiError.code)
      .toBe('INVALID_REQUEST')

    expect(apiError.requestId)
      .toBe('req_del_422')
  })

  it('503 正确读取 Retry-After 和 X-Request-ID', async () => {
    installFetchMock(() =>
      jsonResponse(
        {
          error: {
            code: 'STORAGE_UNAVAILABLE',
            message:
              'Notebook storage is temporarily unavailable',
            requestId: 'req_del_503',
          },
        },
        503,
        {
          'Retry-After': '5',
          'X-Request-ID': 'req_del_503',
        },
      ),
    )

    const error = await deleteNotebook(
      'nb_abc123',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(503)
    expect(apiError.code)
      .toBe('STORAGE_UNAVAILABLE')

    expect(apiError.retryAfter).toBe(5)
    expect(apiError.requestId)
      .toBe('req_del_503')
  })

  it('网络失败转换为 NETWORK_ERROR', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch')
      }),
    )

    const error = await deleteNotebook(
      'nb_abc123',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(0)
    expect(apiError.code).toBe('NETWORK_ERROR')
  })

  it('主动中止保持 AbortError，不伪装成 NETWORK_ERROR', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (
          _input: RequestInfo | URL,
          init?: RequestInit,
        ) => (
          new Promise<Response>(
            (_resolve, reject) => {
              init?.signal?.addEventListener(
                'abort',
                () => {
                  reject(
                    new DOMException(
                      'The operation was aborted',
                      'AbortError',
                    ),
                  )
                },
              )
            },
          )
        ),
      ),
    )

    const controller = new AbortController()

    const pending = deleteNotebook(
      'nb_abc123',
      { signal: controller.signal },
    )

    controller.abort()

    const error = await pending.then(
      () => null,
      (e: unknown) => e,
    )

    expect(error).toBeInstanceOf(DOMException)

    expect((error as DOMException).name)
      .toBe('AbortError')
  })

  it('契约之外的意外 2xx 转换为可诊断错误，不静默接受', async () => {
    installFetchMock(() =>
      new Response(null, {
        status: 200,
        headers: {
          'X-Request-ID': 'req_del_drift',
        },
      }),
    )

    const error = await deleteNotebook(
      'nb_abc123',
    ).then(
      () => null,
      (e: unknown) => e,
    )

    expect(error)
      .toBeInstanceOf(NotebookApiError)

    const apiError =
      error as NotebookApiError

    expect(apiError.status).toBe(200)
    expect(apiError.code).toBe('UNKNOWN')
    expect(apiError.requestId)
      .toBe('req_del_drift')
  })
})
