import {
  KernelManager,
  KernelMessage,
  ServerConnection,
  type Kernel,
} from '@jupyterlab/services'

import type * as nbformat from '@jupyterlab/nbformat'


export interface CellExecutionResult {
  outputs: nbformat.IOutput[]
  executionCount: number | null
}


class ProductJupyterClient {
  private runtimeId:
    string | null = null

  private kernelManager:
    KernelManager | null = null

  private kernel:
    Kernel.IKernelConnection | null = null


  private createServerSettings(
    runtimeId: string,
  ) {
    const httpOrigin =
      window.location.origin

    const wsOrigin =
      httpOrigin.replace(
        /^http/,
        'ws',
      )

    const runtimePath =
      `/runtime-proxy/${runtimeId}/`

    return ServerConnection.makeSettings({
      baseUrl:
        `${httpOrigin}${runtimePath}`,

      wsUrl:
        `${wsOrigin}${runtimePath}`,

      token: '',
    })
  }


  async ensureKernel(
    runtimeId: string,
  ): Promise<Kernel.IKernelConnection> {

    if (
      this.runtimeId === runtimeId
      && this.kernel
      && !this.kernel.isDisposed
    ) {
      return this.kernel
    }

    this.dispose()

    const serverSettings =
      this.createServerSettings(
        runtimeId,
      )

    const manager =
      new KernelManager({
        serverSettings,
      })

    await manager.ready

    const kernel =
      await manager.startNew({
        name: 'python3',
      })

    this.runtimeId = runtimeId
    this.kernelManager = manager
    this.kernel = kernel

    return kernel
  }


  async execute(
    runtimeId: string,
    code: string,
  ): Promise<CellExecutionResult> {

    const kernel =
      await this.ensureKernel(
        runtimeId,
      )

    let outputs:
      nbformat.IOutput[] = []

    let executionCount:
      number | null = null

    const future =
      kernel.requestExecute({
        code,
        stop_on_error: true,
      })

    future.onIOPub = (
      message,
    ) => {
      const type =
        message.header.msg_type

      switch (type) {
        case 'execute_input':
          if (
            KernelMessage.isExecuteInputMsg(
              message,
            )
          ) {
            executionCount =
              message.content.execution_count
          }
          break

        case 'stream':
          if (
            KernelMessage.isStreamMsg(
              message,
            )
          ) {
            outputs.push({
              output_type: 'stream',

              name:
                message.content.name,

              text:
                message.content.text,
            })
          }
          break

        case 'execute_result':
          if (
            KernelMessage.isExecuteResultMsg(
              message,
            )
          ) {
            executionCount =
              message.content.execution_count

            outputs.push({
              output_type:
                'execute_result',

              execution_count:
                message.content.execution_count,

              data:
                message.content.data,

              metadata:
                message.content.metadata,
            })
          }
          break

        case 'display_data':
          if (
            KernelMessage.isDisplayDataMsg(
              message,
            )
          ) {
            outputs.push({
              output_type:
                'display_data',

              data:
                message.content.data,

              metadata:
                message.content.metadata,
            })
          }
          break

        case 'error':
          if (
            KernelMessage.isErrorMsg(
              message,
            )
          ) {
            outputs.push({
              output_type: 'error',

              ename:
                message.content.ename,

              evalue:
                message.content.evalue,

              traceback:
                message.content.traceback,
            })
          }
          break

        case 'clear_output':
          outputs = []
          break
      }
    }

    await future.done

    return {
      outputs,
      executionCount,
    }
  }


  dispose() {
    this.kernel?.dispose()
    this.kernelManager?.dispose()

    this.kernel = null
    this.kernelManager = null
    this.runtimeId = null
  }
}


export const productJupyterClient =
  new ProductJupyterClient()