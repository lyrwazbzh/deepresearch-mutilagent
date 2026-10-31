import { describe, expect, it, vi } from 'vitest'

import { consumeResearchStream } from './research-stream'

function streamFromChunks(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk))
      controller.close()
    },
  })
}

function bytes(value: string): Uint8Array {
  return new TextEncoder().encode(value)
}

function handlers() {
  return {
    onEvent: vi.fn(),
    onProtocolError: vi.fn(),
    onConnectionError: vi.fn(),
  }
}

describe('consumeResearchStream', () => {
  it('decodes UTF-8 characters split across chunks', async () => {
    const encoded = bytes('data: {"type":"phase","content":"研究"}\n\n')
    const splitAt = encoded.indexOf(0xe7) + 1
    const callbacks = handlers()

    await consumeResearchStream(
      streamFromChunks([
        encoded.slice(0, splitAt),
        encoded.slice(splitAt),
      ]),
      callbacks,
    )

    expect(callbacks.onEvent).toHaveBeenCalledWith({
      type: 'phase',
      content: '研究',
    })
    expect(callbacks.onProtocolError).not.toHaveBeenCalled()
  })

  it('supports CRLF, multiple frames, and multiple data lines', async () => {
    const callbacks = handlers()
    const payload = [
      'data: {"type":"phase",',
      'data: "phase":"planning"}',
      '',
      'data: {"type":"phase","phase":"researching"}',
      '',
      '',
    ].join('\r\n')

    await consumeResearchStream(streamFromChunks([bytes(payload)]), callbacks)

    expect(callbacks.onEvent).toHaveBeenNthCalledWith(1, {
      type: 'phase',
      phase: 'planning',
    })
    expect(callbacks.onEvent).toHaveBeenNthCalledWith(2, {
      type: 'phase',
      phase: 'researching',
    })
  })

  it('flushes a trailing frame at EOF and stops at DONE', async () => {
    const callbacks = handlers()

    await consumeResearchStream(
      streamFromChunks([
        bytes(
          'data: {"type":"phase","phase":"writing"}\n\n' +
            'data: [DONE]\n\n' +
            'data: {"type":"phase","phase":"ignored"}',
        ),
      ]),
      callbacks,
    )

    expect(callbacks.onEvent).toHaveBeenCalledTimes(1)
    expect(callbacks.onEvent).toHaveBeenCalledWith({
      type: 'phase',
      phase: 'writing',
    })
  })

  it('emits a final event without a blank-line terminator', async () => {
    const callbacks = handlers()

    await consumeResearchStream(
      streamFromChunks([
        bytes('data: {"type":"phase","phase":"analyzing"}'),
      ]),
      callbacks,
    )

    expect(callbacks.onEvent).toHaveBeenCalledWith({
      type: 'phase',
      phase: 'analyzing',
    })
  })

  it('reports malformed JSON without stopping later events', async () => {
    const callbacks = handlers()

    await consumeResearchStream(
      streamFromChunks([
        bytes(
          'data: {bad json}\n\n' +
            'data: {"type":"phase","phase":"reviewing"}\n\n',
        ),
      ]),
      callbacks,
    )

    expect(callbacks.onProtocolError).toHaveBeenCalledWith(
      expect.any(Error),
      '{bad json}',
    )
    expect(callbacks.onEvent).toHaveBeenCalledWith({
      type: 'phase',
      phase: 'reviewing',
    })
  })

  it('keeps compatibility with chat events that do not have a type', async () => {
    const callbacks = handlers()

    await consumeResearchStream(
      streamFromChunks([
        bytes(
          'event: message\n' +
            'data: {"role":"assistant","content":"hello","thinking":false}\n\n',
        ),
      ]),
      callbacks,
    )

    expect(callbacks.onEvent).toHaveBeenCalledWith({
      role: 'assistant',
      content: 'hello',
      thinking: false,
    })
  })

  it('routes reader failures to the connection error handler', async () => {
    const callbacks = handlers()
    const failure = new Error('connection lost')
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.error(failure)
      },
    })

    await consumeResearchStream(stream, callbacks)

    expect(callbacks.onConnectionError).toHaveBeenCalledWith(failure)
  })
})
