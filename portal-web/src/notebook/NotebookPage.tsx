import {
  isCode,
  isMarkdown,
  type ICell,
} from '@jupyterlab/nbformat'
import { useState } from 'react'

import {
  asNotebookApiError,
  getNotebookRevision,
} from './api'

import { useNotebookDocument } from './useNotebookDocument'

import {
  NotebookApiError,
  type NotebookDocumentResponse,
} from './types'

import {
  runtimeManager,
} from '../runtime/runtimeManager'

import {
  productJupyterClient,
} from '../runtime/productJupyterClient'

import type {
  RuntimeInfo,
} from '../runtime/runtimeClient'


function sourceToText(
  source: string | string[],
): string {
  return Array.isArray(source)
    ? source.join('')
    : source
}


function errorToText(
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

  return parts.join(' · ')
}


export function NotebookPage({
  notebookId,
}: {
  notebookId: string
}) {
  const {
    document,
    loading,
    saving,
    dirty,
    conflict,
    error,
    updateCellSource,
    applyExecutionResult,
    save,
    reload,
  } = useNotebookDocument(notebookId)

  const [
    runtime,
    setRuntime,
  ] = useState<RuntimeInfo | null>(null)

  const [
    runningCellId,
    setRunningCellId,
  ] = useState<string | null>(null)


  /*
   * 历史 revision 只读查看。
   * 独立状态，不替换可编辑 DocumentState，
   * 也不允许直接保存。
   */
  const [
    revisionInput,
    setRevisionInput,
  ] = useState('')

  const [
    historyDocument,
    setHistoryDocument,
  ] = useState<{
    document: NotebookDocumentResponse
    revision: number
    etag: string | null
  } | null>(null)

  const [
    historyLoading,
    setHistoryLoading,
  ] = useState(false)

  const [
    historyError,
    setHistoryError,
  ] = useState<NotebookApiError | null>(null)


  if (loading) {
    return <p>Loading...</p>
  }

  if (!document) {
    /*
     * 初次 GET 失败且没有本地文档：
     * 展示结构化 NotebookApiError
     * （code、message、requestId），
     * 而不是统一显示 Notebook not found。
     */
    return (
      <main
        style={{
          width: '600px',
          margin: '80px auto',
        }}
      >
        <h1>Notebook POC</h1>

        {error
          ? (
            <p style={{ color: 'red' }}>
              {errorToText(error)}
            </p>
          )
          : (
            <p>
              Notebook not found
            </p>
          )}

        <p>
          <a href="/">
            ← 返回首页
          </a>
        </p>
      </main>
    )
  }


  /*
   * 从这里开始，本次 render 中
   * notebookDocument 一定非 null。
   *
   * 同时避免后面的 async function
   * 因闭包而丢失 TypeScript 的 null narrowing。
   */
  const notebookDocument = document


  async function runCell(
    cellId: string,
    source: string,
  ) {
    try {
      setRunningCellId(cellId)

      /*
       * 1. 保证 Notebook 有 READY Runtime
       */
      const readyRuntime =
        await runtimeManager.ensureReady(
          notebookDocument.notebookId,

          (nextRuntime) => {
            setRuntime(nextRuntime)
          },
        )


      /*
       * 2. 通过 Runtime Gateway
       *    连接动态 Jupyter Runtime，
       *    执行当前 Cell source
       */
      const result =
        await productJupyterClient.execute(
          readyRuntime.runtimeId,
          source,
        )


      /*
       * 3. 把 Kernel 返回的 outputs
       *    写回 Notebook Document，
       *    经与普通编辑相同的串行保存路径
       *    保存为新的 revision
       */
      await applyExecutionResult(
        cellId,
        result.outputs,
        result.executionCount,
      )

    } catch (error) {
      console.error(
        'Run cell failed:',
        error,
      )
    } finally {
      setRunningCellId(null)
    }
  }


  async function stopRuntime() {
    try {
      await runtimeManager.stop()

      /*
       * Runtime 已经销毁，
       * 本地 Jupyter Client 连接也一起释放。
       */
      productJupyterClient.dispose()

      setRuntime(null)

    } catch (error) {
      console.error(
        'Stop runtime failed:',
        error,
      )
    }
  }


  async function viewRevision() {
    const revision = Number.parseInt(
      revisionInput,
      10,
    )

    if (
      !Number.isInteger(revision)
      || revision < 1
    ) {
      setHistoryError(
        new NotebookApiError(
          0,
          'UNKNOWN',
          '请输入从 1 开始的整数 revision',
          null,
          null,
          null,
        ),
      )

      return
    }

    setHistoryLoading(true)
    setHistoryError(null)

    try {
      /*
       * 查看同一个 revision 时复用 ETag
       * 做条件读取；新 revision 直接读取。
       */
      const etag =
        historyDocument?.revision === revision
          ? historyDocument.etag
          : null

      const result = await getNotebookRevision(
        notebookDocument.notebookId,
        revision,
        { etag },
      )

      if (result.kind === 'ok') {
        setHistoryDocument({
          document: result.document,
          revision,
          etag: result.etag,
        })
      }
    } catch (viewError) {
      setHistoryError(
        asNotebookApiError(viewError),
      )
    } finally {
      setHistoryLoading(false)
    }
  }


  const stateLabel = conflict
    ? 'Conflict'
    : saving
      ? 'Saving...'
      : dirty
        ? 'Unsaved'
        : 'Saved'


  return (
    <main
      style={{
        width: '900px',
        margin: '40px auto',
      }}
    >
      <header>
        <h1>Notebook POC</h1>

        <div>
          Title:{' '}
          {notebookDocument.title}
        </div>

        <div>
          Notebook:{' '}
          {notebookDocument.notebookId}
        </div>

        <div>
          Revision:{' '}
          {notebookDocument.revision}
        </div>

        <div>
          State: {stateLabel}
        </div>

        <div>
          Runtime:{' '}
          {runtime
            ? `${runtime.state} (${runtime.runtimeId})`
            : 'Disconnected'}
        </div>


        <button
          disabled={!runtime}
          onClick={stopRuntime}
        >
          Stop Runtime
        </button>


        <button
          disabled={
            !dirty
            || saving
            || conflict
          }
          onClick={save}
        >
          {saving
            ? 'Saving...'
            : 'Save'}
        </button>


        <button
          onClick={reload}
          disabled={saving}
        >
          Reload
        </button>


        {conflict && (
          <p
            style={{
              color: 'red',
              fontWeight: 'bold',
            }}
          >
            Notebook 已在其他位置被修改，
            本地修改已保留。
            可点击 Reload 从服务器加载最新版本，
            本地未保存内容将被丢弃。
          </p>
        )}


        {error && !conflict && (
          <p style={{ color: 'red' }}>
            {errorToText(error)}
          </p>
        )}
      </header>


      <hr />


      {notebookDocument.content.cells.map(
        (
          cell: ICell,
          index: number,
        ) => {
          const source =
            sourceToText(cell.source)


          /*
           * 执行当前 Cell 时暂时禁用该 Cell 的
           * source 编辑，避免把旧 source 的输出
           * 显示为新 source 的结果；
           * 其他 Cell 仍可编辑。
           */
          const editingDisabled =
            runningCellId === cell.id


          /*
           * Markdown Cell
           */
          if (isMarkdown(cell)) {
            return (
              <section
                key={cell.id ?? index}
                style={{
                  margin: '24px 0',
                }}
              >
                <strong>
                  Markdown Cell
                </strong>

                <textarea
                  value={source}
                  disabled={editingDisabled}
                  onChange={(event) => {
                    if (cell.id) {
                      updateCellSource(
                        cell.id,
                        event.target.value,
                      )
                    }
                  }}
                  style={{
                    display: 'block',
                    width: '100%',
                    minHeight: 100,
                  }}
                />
              </section>
            )
          }


          /*
           * Code Cell
           */
          if (isCode(cell)) {
            return (
              <section
                key={cell.id ?? index}
                style={{
                  margin: '24px 0',
                }}
              >
                <strong>
                  Code Cell
                </strong>


                <button
                  disabled={
                    !cell.id
                    || runningCellId !== null
                  }
                  onClick={() => {
                    if (!cell.id) {
                      return
                    }

                    runCell(
                      cell.id,
                      source,
                    )
                  }}
                >
                  {
                    runningCellId
                      === cell.id
                      ? 'Running...'
                      : 'Run'
                  }
                </button>


                <textarea
                  value={source}
                  disabled={editingDisabled}
                  onChange={(event) => {
                    if (cell.id) {
                      updateCellSource(
                        cell.id,
                        event.target.value,
                      )
                    }
                  }}
                  style={{
                    display: 'block',
                    width: '100%',
                    minHeight: 120,
                    fontFamily:
                      'monospace',
                  }}
                />


                {cell.outputs?.length > 0 && (
                  <pre
                    style={{
                      background: '#eee',
                      padding: 12,
                    }}
                  >
                    {JSON.stringify(
                      cell.outputs,
                      null,
                      2,
                    )}
                  </pre>
                )}
              </section>
            )
          }


          /*
           * 当前 POC 暂未支持的 Cell 类型
           */
          return (
            <section
              key={index}
            >
              Unsupported cell:{' '}
              {cell.cell_type}
            </section>
          )
        },
      )}


      <hr />


      {/*
       * 历史 revision 只读验证入口。
       * v1 没有版本列表接口，只提供
       * 简单数字输入和只读查看。
       */}
      <section>
        <h2>历史 revision（只读）</h2>

        <input
          type="number"
          min={1}
          placeholder="revision"
          value={revisionInput}
          onChange={(event) =>
            setRevisionInput(
              event.target.value,
            )
          }
        />

        <button
          disabled={historyLoading}
          onClick={viewRevision}
        >
          {historyLoading
            ? 'Loading...'
            : 'View'}
        </button>

        {historyError && (
          <p style={{ color: 'red' }}>
            {errorToText(historyError)}
          </p>
        )}


        {historyDocument && (
          <div
            style={{
              border: '1px solid #ccc',
              padding: 16,
              marginTop: 12,
            }}
          >
            <div>
              Revision:{' '}
              {historyDocument.document.revision}
            </div>

            <div>
              Title:{' '}
              {historyDocument.document.title}
            </div>

            {historyDocument.document.content.cells.map(
              (
                cell: ICell,
                index: number,
              ) => (
                <section
                  key={index}
                  style={{
                    margin: '16px 0',
                  }}
                >
                  <strong>
                    {cell.cell_type} Cell
                  </strong>

                  <pre
                    style={{
                      background: '#f5f5f5',
                      padding: 8,
                    }}
                  >
                    {sourceToText(cell.source)}
                  </pre>

                  {isCode(cell)
                    && cell.outputs?.length > 0
                    && (
                      <pre
                        style={{
                          background: '#f5f5f5',
                          padding: 8,
                        }}
                      >
                        {JSON.stringify(
                          cell.outputs,
                          null,
                          2,
                        )}
                      </pre>
                    )}
                </section>
              ),
            )}
          </div>
        )}
      </section>
    </main>
  )
}
