import type * as nbformat from '@jupyterlab/nbformat'

import {
  asNotebookApiError,
  type SaveNotebookResult,
} from './api'

import type {
  IdempotentOperation,
  NotebookApiError,
  SaveNotebookRequest,
} from './types'


/*
 * 保存协调器所需的浏览器状态快照。
 * 实际发送 PUT 前截取：content 此后不再被修改
 * （DocumentState 每次变化都整体替换 content 对象）。
 */
export interface SaveableDocumentState {
  notebookId: string
  revision: number
  content: nbformat.INotebookContent
  generation: number
  savedGeneration: number
}


export interface SaveCoordinatorDeps {
  /*
   * 读取最新 DocumentState。
   * deps 由调用方在事件/异步上下文中读取，
   * 避免异步回调读到旧的 React closure。
   */
  captureLatest(): SaveableDocumentState | null

  put(
    notebookId: string,
    operation: IdempotentOperation,
  ): Promise<SaveNotebookResult>

  onSaveSuccess(
    result: SaveNotebookResult,
    generation: number,
  ): void

  onError(error: NotebookApiError): void

  onConflict(error: NotebookApiError): void

  onBusyChange(busy: boolean): void
}


/*
 * 结果未知（网络错误 / 5xx / 503）的挂起操作。
 * 结果未知时无论本地 generation/content 如何变化，
 * 下一次 flush 都必须先用原 key、原 body 重放它，
 * 再保存较新的内容。
 */
interface PendingOperation {
  notebookId: string
  operation: IdempotentOperation
  generation: number
  baseRevision: number
}


function isDefinitiveClientError(
  status: number,
): boolean {
  return status >= 400 && status < 500
}


/*
 * 单一串行保存协调器。
 *
 *   Cell source 编辑 ─┐
 *   Kernel outputs  ──┼→ 最新 DocumentState → 协调器 → PUT /api/v1
 *   其他文档变化 ────┘
 *
 * 保证：
 * - 同一个 Notebook 同时最多一个 PUT 在途；
 * - 保存使用快照中的 baseRevision 和完整 content；
 * - 结果未知的请求在下一次 flush 时先以原 key、原 body 重放，
 *   成功后用 pending 自己的 generation 更新 savedGeneration；
 * - 成功后只合并服务端字段，本地较新的 content 不被覆盖；
 * - 若已请求保存较新 generation，重放成功后以服务端返回的
 *   新 revision 为 baseRevision、用新 key 保存最新内容；
 * - 保存期间出现新 generation 时状态保持 dirty；
 * - 已有 run loop 时 flush 返回并等待 active promise，
 *   本次请求触发的排队保存完成后才 resolve；
 * - REVISION_CONFLICT 时保留本地 content、停止 flush、
 *   进入 conflict 状态，直到 reset() 或 reload。
 */
export class SaveCoordinator {
  private pendingOperation:
    PendingOperation | null = null

  private flushRequested = false

  private conflict = false

  private loopPromise:
    Promise<void> | null = null

  private readonly deps:
    () => SaveCoordinatorDeps

  constructor(
    deps: () => SaveCoordinatorDeps,
  ) {
    this.deps = deps
  }


  get hasConflict(): boolean {
    return this.conflict
  }


  /*
   * 冲突恢复（例如从服务器重新加载）后清除
   * 冲突标记和旧内容的挂起操作。
   */
  reset(): void {
    this.pendingOperation = null
    this.conflict = false
  }


  async flush(): Promise<void> {
    if (this.conflict) {
      return
    }

    const latest = this.deps().captureLatest()

    if (
      !latest
      || (
        latest.generation
          === latest.savedGeneration
        && this.pendingOperation === null
      )
    ) {
      return
    }

    this.flushRequested = true

    if (this.loopPromise !== null) {
      /*
       * 已有 run loop 在途：返回 active promise，
       * 本次请求由当前循环继续处理，
       * resolve 时排队保存已完成。
       */
      return this.loopPromise
    }

    const loop = this.runLoop()

    this.loopPromise = loop

    try {
      await loop
    } finally {
      this.loopPromise = null
    }
  }


  private resolveOperation(
    snapshot: SaveableDocumentState,
  ): IdempotentOperation {
    const pending = this.pendingOperation

    if (
      pending
      && pending.generation === snapshot.generation
      && pending.baseRevision === snapshot.revision
    ) {
      /*
       * 同一逻辑写操作因未知结果重试：
       * 复用相同 key 和完全相同的 body。
       */
      return pending.operation
    }

    const request: SaveNotebookRequest = {
      baseRevision: snapshot.revision,
      content: snapshot.content,
    }

    const operation: IdempotentOperation = {
      idempotencyKey: crypto.randomUUID(),
      body: JSON.stringify(request),
    }

    this.pendingOperation = {
      notebookId: snapshot.notebookId,
      operation,
      generation: snapshot.generation,
      baseRevision: snapshot.revision,
    }

    return operation
  }


  private async runLoop(): Promise<void> {
    this.deps().onBusyChange(true)

    try {
      while (!this.conflict) {
        /*
         * 阶段 1：结果未知的 pending 请求必须先用
         * 原 key、原 body 重放，无论本地
         * generation/content 是否已经改变。
         * 成功后用 pending 自己的 generation 更新
         * savedGeneration；本地较新的 content 由
         * 调用方保留，不被服务端响应覆盖。
         */
        const pending = this.pendingOperation

        if (pending) {
          try {
            const result = await this.deps().put(
              pending.notebookId,
              pending.operation,
            )

            this.pendingOperation = null

            this.deps().onSaveSuccess(
              result,
              pending.generation,
            )
          } catch (error) {
            const apiError =
              asNotebookApiError(error)

            if (
              apiError.code
              === 'REVISION_CONFLICT'
            ) {
              this.pendingOperation = null
              this.conflict = true

              this.deps().onConflict(apiError)

              break
            }

            if (
              isDefinitiveClientError(
                apiError.status,
              )
            ) {
              /*
               * 重放得到明确的 4xx：原请求确定失败，
               * head 未被移动，可以继续用当前
               * revision 保存最新内容。
               */
              this.pendingOperation = null
              this.deps().onError(apiError)
            } else {
              /*
               * 网络错误 / 5xx / 503：结果仍未知，
               * 保留 pending，本轮不再继续，
               * 等待下一次 flush 重放。
               */
              this.deps().onError(apiError)

              break
            }
          }
        }


        /*
         * 阶段 2：保存最新 generation。
         */
        if (!this.flushRequested) {
          break
        }

        this.flushRequested = false

        const snapshot =
          this.deps().captureLatest()

        if (!snapshot) {
          break
        }

        if (
          snapshot.generation
          === snapshot.savedGeneration
        ) {
          continue
        }

        const operation =
          this.resolveOperation(snapshot)

        try {
          /*
           * 快照中的 baseRevision 和完整 content
           * 已序列化进 operation.body。
           */
          const result = await this.deps().put(
            snapshot.notebookId,
            operation,
          )

          /*
           * 成功（含 unchanged: true）：
           * 该 operation 得到确定结果，不再复用。
           */
          this.pendingOperation = null

          this.deps().onSaveSuccess(
            result,
            snapshot.generation,
          )
        } catch (error) {
          const apiError =
            asNotebookApiError(error)

          if (
            apiError.code
            === 'REVISION_CONFLICT'
          ) {
            /*
             * 保留本地 content，停止继续 flush，
             * 进入 conflict 状态。
             */
            this.pendingOperation = null
            this.conflict = true

            this.deps().onConflict(apiError)

            break
          }

          if (
            isDefinitiveClientError(
              apiError.status,
            )
          ) {
            /*
             * 明确的 4xx（invalid notebook、
             * payload too large 等）：结束该 operation；
             * 用户修改文档后的新请求使用新 key。
             * 若期间已请求保存更较新 generation，
             * 继续循环保存。
             */
            this.pendingOperation = null
            this.deps().onError(apiError)

            continue
          }

          /*
           * 网络错误 / 5xx / 503：
           * 保留 pending operation 供下一次
           * flush 重放（本轮不做自动重试）。
           */
          this.deps().onError(apiError)

          break
        }
      }
    } finally {
      this.deps().onBusyChange(false)
    }
  }
}
