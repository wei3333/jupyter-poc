import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import type * as nbformat from '@jupyterlab/nbformat'

import {
  asNotebookApiError,
  getNotebook,
  saveNotebook,
  type SaveNotebookResult,
} from './api'

import {
  SaveCoordinator,
  type SaveCoordinatorDeps,
} from './saveCoordinator'

import type {
  NotebookApiError,
  NotebookDocumentResponse,
  NotebookDocumentState,
} from './types'


export function useNotebookDocument(
  notebookId: string,
) {
  const [document, setDocument] =
    useState<NotebookDocumentState | null>(null)

  const [loading, setLoading] =
    useState(true)

  const [saving, setSaving] =
    useState(false)

  const [conflict, setConflict] =
    useState(false)

  const [error, setError] =
    useState<NotebookApiError | null>(null)


  /*
   * ref 保存最新 DocumentState，
   * 避免异步回调读取旧 React closure。
   * ref 只在事件处理与异步回调中访问。
   */
  const documentRef =
    useRef<NotebookDocumentState | null>(null)

  const coordinatorRef =
    useRef<SaveCoordinator | null>(null)


  const commit = useCallback(
    (next: NotebookDocumentState) => {
      documentRef.current = next
      setDocument(next)
    },
    [],
  )


  /*
   * 由服务端文档建立完整 DocumentState。
   * 首次加载 generation = 0，savedGeneration = 0。
   */
  const commitLoadedDocument = useCallback(
    (
      doc: NotebookDocumentResponse,
      etag: string | null,
    ) => {
      commit({
        notebookId: doc.notebookId,
        title: doc.title,
        revision: doc.revision,
        contentHash: doc.contentHash,
        createdAt: doc.createdAt,
        updatedAt: doc.updatedAt,
        etag,
        content: doc.content,
        generation: 0,
        savedGeneration: 0,
      })
    },
    [commit],
  )


  /*
   * 首次加载：本地没有文档，不发送 If-None-Match。
   * 所有 setState 都在 await 之后执行。
   */
  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const result =
          await getNotebook(notebookId)

        if (cancelled) {
          return
        }

        if (result.kind === 'ok') {
          commitLoadedDocument(
            result.document,
            result.etag,
          )
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(
            asNotebookApiError(loadError),
          )
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    load()

    return () => {
      cancelled = true
    }
  }, [notebookId, commitLoadedDocument])


  /*
   * Reload：放弃本地修改，无条件重新读取服务端
   * （不发送 If-None-Match），成功后整体替换
   * DocumentState 并 reset 协调器。
   * 条件 GET（304）能力保留在 API client，
   * 供其他调用方使用。
   */
  const reload = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      const result =
        await getNotebook(notebookId)

      if (result.kind !== 'ok') {
        return
      }

      coordinatorRef.current?.reset()
      setConflict(false)

      commitLoadedDocument(
        result.document,
        result.etag,
      )
    } catch (reloadError) {
      setError(asNotebookApiError(reloadError))
    } finally {
      setLoading(false)
    }
  }, [notebookId, commitLoadedDocument])


  /*
   * Cell 更新使用 v1 保证唯一的 cell.id 定位，
   * 不使用数组 index 作为业务身份。
   */
  const updateCellSource = useCallback(
    (
      cellId: string,
      source: string,
    ) => {
      const current = documentRef.current

      if (!current) {
        return
      }

      const cells = current.content.cells.map(
        (cell) => (
          cell.id === cellId
            ? {
                ...cell,
                source,
              } as nbformat.ICell
            : cell
        ),
      )

      commit({
        ...current,
        generation: current.generation + 1,
        content: {
          ...current.content,
          cells,
        },
      })
    },
    [commit],
  )


  /*
   * 保存协调器依赖。所有成员都稳定，
   * 回调内部在调用时读取 documentRef 最新值。
   */
  const coordinatorDeps = useMemo(
    (): SaveCoordinatorDeps => ({
      captureLatest: () => {
        const current = documentRef.current

        if (!current) {
          return null
        }

        return {
          notebookId: current.notebookId,
          revision: current.revision,
          content: current.content,
          generation: current.generation,
          savedGeneration:
            current.savedGeneration,
        }
      },

      put: saveNotebook,

      onSaveSuccess: (
        result: SaveNotebookResult,
        generation: number,
      ) => {
        const current = documentRef.current

        if (
          !current
          || current.notebookId
            !== result.notebookId
        ) {
          return
        }

        /*
         * 只把服务端字段合并进现有 DocumentState，
         * 不用 PUT 响应整体替换文档；
         * 服务端响应不会覆盖较新的本地 content。
         */
        commit({
          ...current,
          revision: result.revision,
          contentHash: result.contentHash,
          updatedAt: result.updatedAt,
          etag: result.etag,
          savedGeneration: generation,
        })

        setError(null)
      },

      onError: (apiError) => {
        setError(apiError)
      },

      onConflict: (apiError) => {
        setConflict(true)
        setError(apiError)
      },

      onBusyChange: setSaving,
    }),
    [commit],
  )


  /*
   * 协调器在 Notebook 生命周期内只创建一次，
   * 以保留 pending operation 和 conflict 状态；
   * 在事件/异步上下文中惰性创建。
   */
  const ensureCoordinator = useCallback(
    (): SaveCoordinator => {
      let coordinator =
        coordinatorRef.current

      if (coordinator === null) {
        coordinator = new SaveCoordinator(
          () => coordinatorDeps,
        )

        coordinatorRef.current =
          coordinator
      }

      return coordinator
    },
    [coordinatorDeps],
  )


  /*
   * 显式保存与执行结果持久化共用同一个 flush。
   */
  const flush = useCallback(async () => {
    await ensureCoordinator().flush()
  }, [ensureCoordinator])

  const save = flush


  /*
   * 把 Kernel 输出合入最新状态并增加 generation，
   * 然后与普通编辑共用同一条保存路径（flush）。
   */
  const applyExecutionResult = useCallback(
    async (
      cellId: string,
      outputs: nbformat.IOutput[],
      executionCount: number | null,
    ) => {
      const current = documentRef.current

      if (!current) {
        throw new Error('Notebook not loaded')
      }

      const index =
        current.content.cells.findIndex(
          (cell) => cell.id === cellId,
        )

      if (index < 0) {
        throw new Error(
          `Cell not found: ${cellId}`,
        )
      }

      const cell = current.content.cells[index]

      if (cell.cell_type !== 'code') {
        throw new Error(
          'Only code cells can receive outputs',
        )
      }

      const cells = [...current.content.cells]

      cells[index] = {
        ...cell,
        outputs,
        execution_count: executionCount,
      } as nbformat.ICodeCell

      commit({
        ...current,
        generation: current.generation + 1,
        content: {
          ...current.content,
          cells,
        },
      })

      await flush()
    },
    [commit, flush],
  )


  const dirty =
    document !== null
    && document.generation
      !== document.savedGeneration


  return {
    document,
    loading,
    saving,
    dirty,
    conflict,
    error,
    updateCellSource,
    applyExecutionResult,
    save,
    flush,
    reload,
  }
}
