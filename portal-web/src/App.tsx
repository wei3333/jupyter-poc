import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
} from 'react'

import {
  NotebookPage,
} from './notebook/NotebookPage'

import {
  asNotebookApiError,
  createIdempotentOperation,
  createNotebook,
} from './notebook/api'

import {
  buildBlankNotebook,
} from './notebook/notebookContent'

import {
  UploadParseError,
  buildUploadRequest,
  parseNotebookFile,
  type ParsedNotebookFile,
} from './notebook/notebookUpload'

import {
  useNotebookList,
} from './notebook/useNotebookList'

import type {
  CreateNotebookRequest,
  IdempotentOperation,
  NotebookApiError,
} from './notebook/types'


function readNotebookIdFromUrl(
): string | null {
  return new URLSearchParams(
    window.location.search,
  ).get('notebookId')
}


function updateNotebookUrl(
  notebookId: string,
): void {
  const url = new URL(
    window.location.href,
  )

  url.searchParams.set(
    'notebookId',
    notebookId,
  )

  window.history.pushState(
    {},
    '',
    url,
  )
}


function apiErrorText(
  error: NotebookApiError,
): string {
  const parts = [
    `${error.code}: ${error.message}`,
  ]

  if (error.requestId) {
    parts.push(
      `requestId=${error.requestId}`,
    )
  }

  if (error.retryAfter !== null) {
    parts.push(
      `建议 ${error.retryAfter} 秒后重试`,
    )
  }

  const details = error.details

  if (
    typeof details === 'object'
    && details !== null
  ) {
    const d = details as Record<
      string,
      unknown
    >

    if (
      typeof d.path === 'string'
      && typeof d.reason === 'string'
    ) {
      parts.push(
        `path=${d.path} · reason=${d.reason}`,
      )
    }
  }

  return parts.join(' · ')
}


type UploadPhase =
  | 'reading'
  | 'uploading'
  | null


/*
 * 未确定结果的挂起上传。
 * 网络失败 / 500 / 503 后保留，
 * 用户点击重试时复用同一 key 和 body。
 */
interface PendingUpload {
  operation: IdempotentOperation
}


type UploadError =
  | { kind: 'local'; message: string }
  | { kind: 'api'; error: NotebookApiError }


function HomePage({
  onOpen,
}: {
  onOpen: (notebookId: string) => void
}) {
  const list = useNotebookList()

  const [openInput, setOpenInput] =
    useState('')


  /*
   * 普通创建。
   */
  const [creating, setCreating] =
    useState(false)

  const [createError, setCreateError] =
    useState<NotebookApiError | null>(null)

  /*
   * 未确定结果的创建 operation。
   * 响应丢失后再次点击创建时复用同一 key，
   * 避免产生重复 Notebook。
   */
  const [createOperation, setCreateOperation] =
    useState<IdempotentOperation | null>(null)


  /*
   * 本地 .ipynb 上传。
   * 上传是独立于普通创建的另一种逻辑创建操作，
   * 两者各自保留 pending operation，不能共享。
   */
  const [uploadPhase, setUploadPhase] =
    useState<UploadPhase>(null)

  const [uploadError, setUploadError] =
    useState<UploadError | null>(null)

  const [uploadPending, setUploadPending] =
    useState<PendingUpload | null>(null)

  const [uploadedFileName, setUploadedFileName] =
    useState<string | null>(null)

  const fileInputRef =
    useRef<HTMLInputElement | null>(null)


  const uploadBusy = uploadPhase !== null


  async function handleCreate() {
    if (creating || uploadBusy) {
      return
    }

    setCreating(true)
    setCreateError(null)

    let operation = createOperation

    if (!operation) {
      /*
       * 新的逻辑创建：只构造一次模板和一次
       * IdempotentOperation。
       */
      const request: CreateNotebookRequest = {
        content: buildBlankNotebook(),
      }

      operation =
        createIdempotentOperation(request)

      setCreateOperation(operation)
    }

    try {
      const result = await createNotebook(
        operation,
      )

      setCreateOperation(null)

      onOpen(result.document.notebookId)
    } catch (error) {
      const apiError =
        asNotebookApiError(error)

      if (
        apiError.status >= 400
        && apiError.status < 500
      ) {
        /*
         * 明确的 4xx：操作已有确定结果，
         * 下次点击创建时使用新 key。
         */
        setCreateOperation(null)
      }

      setCreateError(apiError)
    } finally {
      setCreating(false)
    }
  }


  function resetFileInput(
    input: HTMLInputElement | null,
  ): void {
    if (input) {
      input.value = ''
    }
  }


  async function submitUpload(
    pending: PendingUpload,
    fileInput: HTMLInputElement | null,
  ): Promise<void> {
    setUploadPhase('uploading')
    setUploadError(null)

    try {
      const result = await createNotebook(
        pending.operation,
      )

      /*
       * 成功：清除 operation 和文件选择状态，
       * 通过响应 notebookId 进入编辑页。
       * 不用原始上传对象覆盖服务端规范化响应，
       * 编辑页重新 GET 权威内容。
       */
      setUploadPending(null)
      resetFileInput(fileInput)

      onOpen(result.document.notebookId)
    } catch (error) {
      const apiError =
        asNotebookApiError(error)

      if (
        apiError.status >= 400
        && apiError.status < 500
      ) {
        /*
         * 明确的 4xx（含 INVALID_NOTEBOOK、
         * PAYLOAD_TOO_LARGE）结束该 operation；
         * 用户改选文件意味着新的逻辑上传。
         * 清空 file input 使同一文件能重新选择。
         */
        setUploadPending(null)
        resetFileInput(fileInput)
      }

      /*
       * 网络失败 / 500 / 503 保留 pending
       * operation，供“重试上传”复用。
       */
      setUploadError({
        kind: 'api',
        error: apiError,
      })
    } finally {
      setUploadPhase(null)
    }
  }


  async function handleFileSelected(
    event: ChangeEvent<HTMLInputElement>,
  ) {
    const input = event.target

    const file = input.files?.[0]

    if (!file || creating) {
      return
    }

    /*
     * 用户改选文件意味着开始新的逻辑上传：
     * 清除上一文件的 pending operation 和本地错误。
     */
    setUploadPending(null)
    setUploadError(null)
    setUploadedFileName(file.name)

    setUploadPhase('reading')


    let parsed: ParsedNotebookFile

    try {
      const text = await file.text()

      parsed = parseNotebookFile(
        file.name,
        text,
      )
    } catch (error) {
      /*
       * 本地文件读取 / JSON 解析错误属于
       * 上传 UI 错误，不发送 POST。
       * 清空 file input 使同一文件能重新选择。
       */
      setUploadError(
        error instanceof UploadParseError
          ? {
              kind: 'local',
              message: error.message,
            }
          : {
              kind: 'local',
              message: '无法读取本地文件',
            },
      )

      setUploadPhase(null)
      resetFileInput(input)

      return
    }


    /*
     * 文件解析成功后只构造一次
     * {title, content} 和一次 IdempotentOperation。
     */
    const request = buildUploadRequest(parsed)

    const pending: PendingUpload = {
      operation:
        createIdempotentOperation(request),
    }

    setUploadPending(pending)

    await submitUpload(pending, input)
  }


  async function retryUpload() {
    if (
      !uploadPending
      || uploadBusy
      || creating
    ) {
      return
    }

    /*
     * 未知结果重试：复用同一 key 和
     * 已经序列化的 body。
     */
    await submitUpload(
      uploadPending,
      fileInputRef.current,
    )
  }


  const createDisabled =
    creating || uploadBusy

  const uploadDisabled =
    uploadBusy || creating


  return (
    <main
      style={{
        width: '720px',
        margin: '40px auto',
      }}
    >
      <h1>Notebook POC</h1>


      <section>
        <h2>Notebooks</h2>

        {list.initialLoading
          ? (
            <p>Loading...</p>
          )
          : list.items.length === 0
            ? (
              list.listError
                && list.errorPhase === 'initial'
                ? (
                  <div>
                    <p style={{ color: 'red' }}>
                      {apiErrorText(list.listError)}
                    </p>

                    <button
                      onClick={list.refresh}
                    >
                      重试
                    </button>
                  </div>
                )
                : (
                  <p>
                    暂无 Notebook。
                    可以创建或上传一个。
                  </p>
                )
            )
            : (
              <div>
                <ul>
                  {list.items.map((item) => (
                    <li
                      key={item.notebookId}
                      style={{
                        margin: '8px 0',
                      }}
                    >
                      <button
                        onClick={() =>
                          onOpen(
                            item.notebookId,
                          )
                        }
                      >
                        <strong>
                          {item.title}
                        </strong>
                        {' — '}
                        revision {item.revision}
                        {' · '}
                        {item.updatedAt}
                        {' · '}
                        {item.notebookId}
                      </button>
                    </li>
                  ))}
                </ul>

                {list.listError
                  && list.errorPhase === 'initial'
                  && (
                    <p style={{ color: 'red' }}>
                      {apiErrorText(
                        list.listError,
                      )}
                    </p>
                  )}

                {list.nextCursor !== null
                  && (
                    <button
                      disabled={
                        list.loadingMore
                      }
                      onClick={list.loadMore}
                    >
                      {list.loadingMore
                        ? 'Loading...'
                        : '加载更多'}
                    </button>
                  )}

                {list.listError
                  && list.errorPhase === 'loadMore'
                  && (
                    <p style={{ color: 'red' }}>
                      {list.listError.code
                        === 'INVALID_CURSOR'
                        ? '分页 cursor 已失效，请点击刷新重新加载第一页。'
                        : apiErrorText(
                            list.listError,
                          )}
                    </p>
                  )}
              </div>
            )}


        <p>
          <button
            disabled={
              list.initialLoading
              || list.loadingMore
            }
            onClick={list.refresh}
          >
            刷新
          </button>
        </p>
      </section>


      <hr />


      <section>
        <h2>创建 Notebook</h2>

        <button
          disabled={createDisabled}
          onClick={handleCreate}
        >
          {creating
            ? 'Creating...'
            : 'Create Notebook'}
        </button>

        {createError && (
          <p style={{ color: 'red' }}>
            {apiErrorText(createError)}
          </p>
        )}
      </section>


      <hr />


      <section>
        <h2>上传 .ipynb</h2>

        <input
          ref={fileInputRef}
          type="file"
          accept=".ipynb,application/json"
          disabled={uploadDisabled}
          onChange={handleFileSelected}
        />

        {uploadPhase === 'reading' && (
          <p>正在读取本地文件...</p>
        )}

        {uploadPhase === 'uploading' && (
          <p>正在上传...</p>
        )}

        {uploadedFileName && (
          <p>
            已选择：{uploadedFileName}
          </p>
        )}

        {uploadError?.kind === 'local' && (
          <p style={{ color: 'red' }}>
            本地文件错误：{uploadError.message}
          </p>
        )}

        {uploadError?.kind === 'api' && (
          <div>
            <p style={{ color: 'red' }}>
              {apiErrorText(uploadError.error)}
            </p>

            {uploadPending !== null && (
              <button
                disabled={uploadDisabled}
                onClick={retryUpload}
              >
                重试上传
              </button>
            )}
          </div>
        )}
      </section>


      <hr />


      {/*
       * 按 Notebook ID 打开的 POC 诊断入口。
       * 列表是主要打开入口。
       */}
      <section>
        <h2>按 ID 打开（诊断）</h2>

        <input
          placeholder="notebookId"
          value={openInput}
          onChange={(event) =>
            setOpenInput(
              event.target.value,
            )
          }
        />

        <button
          disabled={!openInput.trim()}
          onClick={() => {
            const id = openInput.trim()

            if (id) {
              onOpen(id)
            }
          }}
        >
          Open
        </button>
      </section>
    </main>
  )
}


function App() {
  const [notebookId, setNotebookId] =
    useState<string | null>(() =>
      readNotebookIdFromUrl(),
    )


  useEffect(() => {
    const onPopState = () => {
      setNotebookId(
        readNotebookIdFromUrl(),
      )
    }

    window.addEventListener(
      'popstate',
      onPopState,
    )

    return () => {
      window.removeEventListener(
        'popstate',
        onPopState,
      )
    }
  }, [])


  const openNotebook = (
    id: string,
  ) => {
    updateNotebookUrl(id)
    setNotebookId(id)
  }


  if (notebookId) {
    return (
      <NotebookPage
        key={notebookId}
        notebookId={notebookId}
      />
    )
  }


  /*
   * 首页组件在 notebookId 从非空回到空时
   * 重新挂载，自动重新拉取第一页，
   * 新创建的 Notebook 会出现在列表顶部。
   */
  return (
    <HomePage
      onOpen={openNotebook}
    />
  )
}

export default App
