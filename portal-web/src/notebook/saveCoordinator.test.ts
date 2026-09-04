import {
  describe,
  expect,
  it,
} from 'vitest'

import type * as nbformat from '@jupyterlab/nbformat'

import {
  NotebookApiError,
  type IdempotentOperation,
  type SaveNotebookRequest,
} from './types'

import type { SaveNotebookResult } from './api'

import {
  SaveCoordinator,
  type SaveableDocumentState,
  type SaveCoordinatorDeps,
} from './saveCoordinator'


function notebookWith(
  source: string,
): nbformat.INotebookContent {
  return {
    nbformat: 4,
    nbformat_minor: 5,
    metadata: {},
    cells: [
      {
        id: 'cell_1',
        cell_type: 'code',
        metadata: {},
        source,
        execution_count: null,
        outputs: [],
      },
    ],
  }
}


interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (error: unknown) => void
}


function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void

  const promise = new Promise<T>(
    (res, rej) => {
      resolve = res
      reject = rej
    },
  )

  return { promise, resolve, reject }
}


/*
 * 让 resolve/reject 之后的 promise 链
 * （finally + 协调器的 await 续体）跑完。
 */
async function settle(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
  await Promise.resolve()
}


interface RecordedPut {
  notebookId: string
  operation: IdempotentOperation
}


function requestBodyOf(
  put: RecordedPut,
): SaveNotebookRequest {
  return JSON.parse(
    put.operation.body,
  ) as SaveNotebookRequest
}


/*
 * 模拟 hook 与协调器的交互：
 * - state 是最新 DocumentState（content 只整体替换）；
 * - put 由测试手动控制 resolve/reject；
 * - onSaveSuccess 按 hook 的语义只合并服务端字段。
 */
class Harness {
  state: SaveableDocumentState = {
    notebookId: 'nb_abc123',
    revision: 1,
    content: notebookWith('print(1)'),
    generation: 0,
    savedGeneration: 0,
  }

  puts: RecordedPut[] = []

  successes: {
    result: SaveNotebookResult
    generation: number
  }[] = []

  errors: NotebookApiError[] = []

  conflicts: NotebookApiError[] = []

  busyLog: boolean[] = []

  private putDeferreds:
    Deferred<SaveNotebookResult>[] = []

  private inFlight = 0

  maxInFlight = 0

  readonly coordinator: SaveCoordinator


  constructor() {
    this.coordinator = new SaveCoordinator(
      (): SaveCoordinatorDeps => ({
        captureLatest: () => this.state,

        put: (
          notebookId,
          operation,
        ) => {
          const d =
            deferred<SaveNotebookResult>()

          this.putDeferreds.push(d)

          this.inFlight += 1

          this.maxInFlight = Math.max(
            this.maxInFlight,
            this.inFlight,
          )

          this.puts.push({
            notebookId,
            operation,
          })

          return d.promise.finally(() => {
            this.inFlight -= 1
          })
        },

        onSaveSuccess: (
          result,
          generation,
        ) => {
          /*
           * 与 hook 相同：只合并服务端字段，
           * 不覆盖本地 content。
           */
          this.state = {
            ...this.state,
            revision: result.revision,
            savedGeneration: generation,
          }

          this.successes.push({
            result,
            generation,
          })
        },

        onError: (error) => {
          this.errors.push(error)
        },

        onConflict: (error) => {
          this.conflicts.push(error)
        },

        onBusyChange: (busy) => {
          this.busyLog.push(busy)
        },
      }),
    )
  }


  edit(source: string): void {
    this.state = {
      ...this.state,
      content: notebookWith(source),
      generation: this.state.generation + 1,
    }
  }


  resolveLastPut(
    result: Partial<SaveNotebookResult> = {},
  ): void {
    const d = this.putDeferreds.pop()

    if (!d) {
      throw new Error('No pending PUT')
    }

    d.resolve(this.saveResult(result))
  }


  rejectLastPut(
    status: number,
    code: NotebookApiError['code'],
  ): void {
    const d = this.putDeferreds.pop()

    if (!d) {
      throw new Error('No pending PUT')
    }

    d.reject(
      new NotebookApiError(
        status,
        code,
        'test error',
        `req_${code}`,
        null,
        null,
      ),
    )
  }


  private saveResult(
    overrides: Partial<SaveNotebookResult>,
  ): SaveNotebookResult {
    return {
      notebookId: this.state.notebookId,
      revision: this.state.revision + 1,
      contentHash:
        'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
      updatedAt: '2026-09-04T02:00:00Z',
      unchanged: false,
      etag: null,
      requestId: 'req_ok',
      ...overrides,
    }
  }
}


describe('SaveCoordinator', () => {
  it('同时调用两次 flush 不会产生并发 PUT', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const first = harness.coordinator.flush()

    /*
     * 第一个 PUT 在途时再次 flush，
     * 不应启动第二个并发 PUT。
     */
    const second = harness.coordinator.flush()

    await Promise.resolve()
    await Promise.resolve()

    expect(harness.puts.length).toBe(1)

    expect(harness.maxInFlight).toBe(1)

    harness.resolveLastPut()

    await Promise.all([first, second])

    /*
     * 第二次 flush 请求的 generation
     * 已被第一个 PUT 保存，无需再发。
     */
    expect(harness.puts.length).toBe(1)

    expect(
      harness.busyLog.filter((busy) => busy)
        .length,
    ).toBe(1)
  })


  it('保存期间出现的新编辑不会被旧响应清除 dirty', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const flushPromise =
      harness.coordinator.flush()

    await Promise.resolve()

    /*
     * 第一个 PUT 在途时用户继续编辑：
     * generation 增加，content 更新。
     */
    harness.edit('print(2)')

    expect(harness.puts.length).toBe(1)

    harness.resolveLastPut()

    await flushPromise

    /*
     * 旧响应只合并服务端字段：
     * savedGeneration = 快照的 generation（1），
     * 本地 content（print(2)）不被覆盖，
     * 状态仍然 dirty。
     */
    expect(harness.state.generation).toBe(2)
    expect(harness.state.savedGeneration)
      .toBe(1)

    expect(harness.state.content.cells[0].source)
      .toBe('print(2)')
  })


  it('保存期间的显式 flush 在当前请求结束后保存最新 generation', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const first = harness.coordinator.flush()

    await Promise.resolve()

    harness.edit('print(2)')

    /*
     * 编辑后用户显式点击 Save。
     */
    const second = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(1)

    harness.resolveLastPut({
      revision: 2,
      etag: '"nb_abc123-r2"',
    })

    await settle()

    /*
     * 第一个 PUT 结束后继续保存最新 generation。
     */
    expect(harness.puts.length).toBe(2)

    expect(harness.maxInFlight).toBe(1)

    expect(
      requestBodyOf(harness.puts[1]).baseRevision,
    ).toBe(2)

    expect(
      requestBodyOf(harness.puts[1])
        .content.cells[0].source,
    ).toBe('print(2)')

    /*
     * 内容已经改变：第二次 PUT 使用新 key。
     */
    expect(harness.puts[1].operation.idempotencyKey)
      .not.toBe(
        harness.puts[0].operation.idempotencyKey,
      )

    harness.resolveLastPut({ revision: 3 })

    await Promise.all([first, second])

    expect(harness.state.revision).toBe(3)
    expect(harness.state.savedGeneration).toBe(2)
  })


  it('网络错误和 5xx 后保留 pending operation，重试复用同一 key 和 body', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const first = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(1)

    harness.rejectLastPut(
      503,
      'STORAGE_UNAVAILABLE',
    )

    await first

    expect(harness.state.savedGeneration).toBe(0)

    expect(harness.errors.length).toBe(1)

    expect(harness.errors[0].code)
      .toBe('STORAGE_UNAVAILABLE')


    /*
     * 没有新编辑，用户再次保存：
     * 复用同一 key 和完全相同的 body。
     */
    const second = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(2)

    expect(harness.puts[1].operation.idempotencyKey)
      .toBe(
        harness.puts[0].operation.idempotencyKey,
      )

    expect(harness.puts[1].operation.body)
      .toBe(harness.puts[0].operation.body)

    harness.resolveLastPut({ revision: 2 })

    await second

    expect(harness.state.savedGeneration).toBe(1)
  })


  it('结果未知的 pending 请求先重放，再以新 revision 保存新内容', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const first = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(1)

    const originalKey =
      harness.puts[0].operation.idempotencyKey

    const originalBody =
      harness.puts[0].operation.body

    /*
     * 请求 A 网络结果未知。
     */
    harness.rejectLastPut(
      503,
      'STORAGE_UNAVAILABLE',
    )

    await first


    /*
     * 用户编辑 B 后再次 flush：
     * 必须先重放 A，再保存 B。
     */
    harness.edit('print(2)')

    const second = harness.coordinator.flush()

    await settle()

    expect(harness.puts.length).toBe(2)

    /*
     * 第一次发出的必须是 A 的重放：
     * 原 key + 原 body。
     */
    expect(harness.puts[1].operation.idempotencyKey)
      .toBe(originalKey)

    expect(harness.puts[1].operation.body)
      .toBe(originalBody)


    /*
     * A 重放成功：savedGeneration 使用 A 自己的
     * generation（1），本地较新的 content
     * （print(2)）保留不被覆盖。
     */
    harness.resolveLastPut({ revision: 2 })

    await settle()

    expect(harness.state.savedGeneration).toBe(1)

    expect(harness.state.generation).toBe(2)

    expect(harness.state.content.cells[0].source)
      .toBe('print(2)')


    /*
     * 随后以服务端返回的新 revision 为 baseRevision、
     * 使用新 key 保存最新内容。
     */
    expect(harness.puts.length).toBe(3)

    expect(harness.puts[2].operation.idempotencyKey)
      .not.toBe(originalKey)

    expect(
      requestBodyOf(harness.puts[2]).baseRevision,
    ).toBe(2)

    expect(
      requestBodyOf(harness.puts[2])
        .content.cells[0].source,
    ).toBe('print(2)')

    harness.resolveLastPut({ revision: 3 })

    await second

    expect(harness.state.revision).toBe(3)
    expect(harness.state.savedGeneration).toBe(2)
  })


  it('已有 run loop 时 flush 等待 active promise，排队保存完成后才 resolve', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    let firstResolved = false
    let secondResolved = false

    const first = harness.coordinator.flush()

    first.then(() => {
      firstResolved = true
    })

    await Promise.resolve()

    expect(harness.puts.length).toBe(1)


    /*
     * 第一个 PUT 在途时编辑并再次 flush。
     */
    harness.edit('print(2)')

    const second = harness.coordinator.flush()

    second.then(() => {
      secondResolved = true
    })


    /*
     * 第一个 PUT 成功后循环继续保存 print(2)：
     * 此时两个 flush 的 promise 都不能提前 resolve。
     */
    harness.resolveLastPut({ revision: 2 })

    await settle()

    expect(harness.puts.length).toBe(2)

    expect(firstResolved).toBe(false)
    expect(secondResolved).toBe(false)


    harness.resolveLastPut({ revision: 3 })

    await Promise.all([first, second])

    expect(firstResolved).toBe(true)
    expect(secondResolved).toBe(true)

    expect(harness.state.savedGeneration).toBe(2)
  })


  it('REVISION_CONFLICT 保留本地内容并停止继续 flush', async () => {
    const harness = new Harness()

    harness.edit('local change')

    const localContent = harness.state.content

    const first = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(1)

    harness.rejectLastPut(
      409,
      'REVISION_CONFLICT',
    )

    await first


    expect(harness.conflicts.length).toBe(1)

    expect(harness.conflicts[0].code)
      .toBe('REVISION_CONFLICT')

    expect(harness.coordinator.hasConflict)
      .toBe(true)


    /*
     * 本地 content 仍然保留。
     */
    expect(harness.state.content)
      .toBe(localContent)


    /*
     * 冲突后停止继续 flush：
     * 再次保存不再发出 PUT。
     */
    await harness.coordinator.flush()

    expect(harness.puts.length).toBe(1)


    /*
     * 从服务器重新加载（reset）后恢复。
     */
    harness.coordinator.reset()

    harness.state = {
      ...harness.state,
      revision: 3,
      savedGeneration:
        harness.state.generation,
    }

    harness.edit('after reload')

    const retry = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(2)

    expect(
      requestBodyOf(harness.puts[1]).baseRevision,
    ).toBe(3)

    harness.resolveLastPut({ revision: 4 })

    await retry
  })


  it('PUT 返回 unchanged: true 时按成功处理且 revision 可能不变', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const flushPromise =
      harness.coordinator.flush()

    await Promise.resolve()

    harness.resolveLastPut({
      revision: 1,
      unchanged: true,
      etag: '"nb_abc123-r1"',
    })

    await flushPromise

    expect(harness.successes.length).toBe(1)

    expect(harness.successes[0].result.unchanged)
      .toBe(true)

    expect(harness.state.revision).toBe(1)

    expect(harness.state.savedGeneration).toBe(1)

    expect(harness.coordinator.hasConflict)
      .toBe(false)

    expect(harness.errors.length).toBe(0)
  })


  it('明确的 4xx 结束该 operation，重试使用新 key', async () => {
    const harness = new Harness()

    harness.edit('print(1)')

    const first = harness.coordinator.flush()

    await Promise.resolve()

    harness.rejectLastPut(
      422,
      'INVALID_NOTEBOOK',
    )

    await first

    expect(harness.puts.length).toBe(1)


    /*
     * 用户修复文档后再次保存：
     * 新 key（4xx 后 operation 不再复用）。
     */
    harness.edit('print(2)')

    const second = harness.coordinator.flush()

    await Promise.resolve()

    expect(harness.puts.length).toBe(2)

    expect(harness.puts[1].operation.idempotencyKey)
      .not.toBe(
        harness.puts[0].operation.idempotencyKey,
      )

    harness.resolveLastPut({ revision: 2 })

    await second

    expect(harness.state.savedGeneration).toBe(2)
  })


  it('没有本地修改时 flush 不发送 PUT', async () => {
    const harness = new Harness()

    await harness.coordinator.flush()

    expect(harness.puts.length).toBe(0)
    expect(harness.busyLog.length).toBe(0)
  })
})
