import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'

import {
  asNotebookApiError,
  isAbortError,
  listNotebooks,
} from './api'

import type {
  NotebookApiError,
  NotebookSummary,
} from './types'


export type ListErrorPhase =
  | 'initial'
  | 'loadMore'


const PAGE_LIMIT = 20


function appendUnique(
  current: NotebookSummary[],
  incoming: NotebookSummary[],
): NotebookSummary[] {
  /*
   * 追加时按 notebookId 防御性去重，
   * 仍保持服务端返回的相对顺序。
   */
  const seen = new Set(
    current.map((item) => item.notebookId),
  )

  return [
    ...current,
    ...incoming.filter(
      (item) => !seen.has(item.notebookId),
    ),
  ]
}


export function useNotebookList() {
  const [items, setItems] =
    useState<NotebookSummary[]>([])

  const [nextCursor, setNextCursor] =
    useState<string | null>(null)

  const [initialLoading, setInitialLoading] =
    useState(true)

  const [loadingMore, setLoadingMore] =
    useState(false)

  const [listError, setListError] =
    useState<NotebookApiError | null>(null)

  const [errorPhase, setErrorPhase] =
    useState<ListErrorPhase | null>(null)


  const abortRef =
    useRef<AbortController | null>(null)

  /*
   * 请求序号：只有最新一轮请求可以更新状态，
   * 被中止或过期的请求不得覆盖页面状态。
   */
  const seqRef = useRef(0)


  /*
   * 首次显示时请求第一页：limit=20，不带 cursor。
   * setState 全部在 await 之后执行，
   * 且与 refresh 一样做 seq 一致性检查：
   * 过期请求不得覆盖页面状态。
   */
  useEffect(() => {
    const controller = new AbortController()

    abortRef.current = controller

    const seq = seqRef.current + 1
    seqRef.current = seq

    let cancelled = false

    async function loadFirstPage() {
      try {
        const result = await listNotebooks({
          limit: PAGE_LIMIT,
          signal: controller.signal,
        })

        if (
          cancelled
          || seq !== seqRef.current
        ) {
          return
        }

        setItems(result.items)
        setNextCursor(result.nextCursor)
        setListError(null)
        setErrorPhase(null)
      } catch (error) {
        if (
          !cancelled
          && seq === seqRef.current
          && !isAbortError(error)
        ) {
          setListError(
            asNotebookApiError(error),
          )

          setErrorPhase('initial')
        }
      } finally {
        if (
          !cancelled
          && seq === seqRef.current
        ) {
          setInitialLoading(false)
        }
      }
    }

    loadFirstPage()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [])


  /*
   * 刷新：不带 cursor 重新请求第一页，
   * 整体替换 items 和 nextCursor。
   * 中止上一轮仍在途的请求。
   */
  const refresh = useCallback(async () => {
    abortRef.current?.abort()

    const controller = new AbortController()

    abortRef.current = controller

    const seq = seqRef.current + 1
    seqRef.current = seq

    setInitialLoading(true)
    setListError(null)
    setErrorPhase(null)

    try {
      const result = await listNotebooks({
        limit: PAGE_LIMIT,
        signal: controller.signal,
      })

      if (seq !== seqRef.current) {
        return
      }

      setItems(result.items)
      setNextCursor(result.nextCursor)
    } catch (error) {
      if (
        seq !== seqRef.current
        || isAbortError(error)
      ) {
        return
      }

      setListError(asNotebookApiError(error))
      setErrorPhase('initial')
    } finally {
      if (seq === seqRef.current) {
        setInitialLoading(false)
      }
    }
  }, [])


  /*
   * 加载更多：以当前 cursor 请求下一页并追加。
   * INVALID_CURSOR 时不再循环重试旧 cursor：
   * 保留已有列表、置空 nextCursor，提示刷新第一页。
   */
  const loadMore = useCallback(async () => {
    if (
      loadingMore
      || nextCursor === null
    ) {
      return
    }

    setLoadingMore(true)
    setListError(null)
    setErrorPhase(null)

    try {
      const result = await listNotebooks({
        limit: PAGE_LIMIT,
        cursor: nextCursor,
      })

      setItems((current) =>
        appendUnique(current, result.items),
      )

      setNextCursor(result.nextCursor)
    } catch (error) {
      const apiError =
        asNotebookApiError(error)

      if (apiError.code === 'INVALID_CURSOR') {
        setNextCursor(null)
      }

      setListError(apiError)
      setErrorPhase('loadMore')
    } finally {
      setLoadingMore(false)
    }
  }, [loadingMore, nextCursor])


  return {
    items,
    nextCursor,
    initialLoading,
    loadingMore,
    listError,
    errorPhase,
    refresh,
    loadMore,
  }
}
