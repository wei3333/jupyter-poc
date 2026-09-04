import {
  useEffect,
  useRef,
  useState,
} from 'react'

import type * as nbformat from '@jupyterlab/nbformat'

import {
  NotebookPage,
} from './notebook/NotebookPage'

import {
  asNotebookApiError,
  createIdempotentOperation,
  createNotebook,
} from './notebook/api'

import type {
  CreateNotebookRequest,
  IdempotentOperation,
  NotebookApiError,
} from './notebook/types'


/*
 * 产品侧最小验证模板：
 * nbformat 4.5 + 一个空 code Cell。
 * Cell ID 使用去掉连字符的 crypto.randomUUID()。
 * 模板属于 Portal，不写入 Notebook Service。
 */
function buildTemplateContent(
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


function readNotebookIdFromUrl(
): string | null {
  return new URLSearchParams(
    window.location.search,
  ).get('notebookId')
}


function buildTemplateRequest(
): CreateNotebookRequest {
  return {
    content: buildTemplateContent(),
  }
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


function App() {
  const [notebookId, setNotebookId] =
    useState<string | null>(() =>
      readNotebookIdFromUrl(),
    )

  const [openInput, setOpenInput] =
    useState('')

  const [creating, setCreating] =
    useState(false)

  const [createError, setCreateError] =
    useState<NotebookApiError | null>(null)


  /*
   * 未确定结果的创建 operation。
   * 响应丢失后再次点击创建时复用同一 key，
   * 避免产生重复 Notebook。
   */
  const createOperationRef =
    useRef<IdempotentOperation | null>(null)


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


  async function handleCreate() {
    setCreating(true)
    setCreateError(null)

    const request = buildTemplateRequest()

    /*
     * 有挂起的 operation（上次结果未知）时复用；
     * body 已在 operation 创建时固定，
     * 重试时重新生成的模板不会进入请求。
     */
    let operation =
      createOperationRef.current

    if (!operation) {
      operation = createIdempotentOperation(
        request,
      )

      createOperationRef.current =
        operation
    }

    try {
      const result = await createNotebook(
        operation,
      )

      createOperationRef.current = null

      const notebookId =
        result.document.notebookId

      updateNotebookUrl(notebookId)
      setNotebookId(notebookId)
    } catch (createError) {
      const apiError =
        asNotebookApiError(createError)

      if (
        apiError.status >= 400
        && apiError.status < 500
      ) {
        /*
         * 明确的 4xx：操作已有确定结果，
         * 下次点击创建时使用新 key。
         */
        createOperationRef.current = null
      }

      setCreateError(apiError)
    } finally {
      setCreating(false)
    }
  }


  function handleOpen() {
    const id = openInput.trim()

    if (!id) {
      return
    }

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
   * v1 尚无列表接口，
   * 首页只提供创建验证 Notebook
   * 和按 ID 打开两个入口。
   */
  return (
    <main
      style={{
        width: '600px',
        margin: '80px auto',
      }}
    >
      <h1>Notebook POC</h1>

      <section
        style={{
          margin: '32px 0',
        }}
      >
        <button
          disabled={creating}
          onClick={handleCreate}
        >
          {creating
            ? 'Creating...'
            : 'Create Notebook'}
        </button>

        {createError && (
          <p style={{ color: 'red' }}>
            {createError.code}:{' '}
            {createError.message}
            {createError.requestId
              ? ` · requestId=${createError.requestId}`
              : ''}
          </p>
        )}
      </section>

      <hr />

      <section
        style={{
          margin: '32px 0',
        }}
      >
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
          onClick={handleOpen}
        >
          Open
        </button>
      </section>
    </main>
  )
}

export default App
