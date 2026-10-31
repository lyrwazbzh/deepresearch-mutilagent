import type { ResearchEvent } from './types'

interface ResearchStreamHandlers {
  onEvent: (event: ResearchEvent) => void
  onProtocolError: (error: Error, raw: string) => void
  onConnectionError: (error: Error) => void
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

function readData(frame: string): string | null {
  const dataLines = frame
    .split(/\r?\n/)
    .filter((line) => line === 'data' || line.startsWith('data:'))
    .map((line) => {
      const value = line === 'data' ? '' : line.slice(5)
      return value.startsWith(' ') ? value.slice(1) : value
    })

  return dataLines.length > 0 ? dataLines.join('\n') : null
}

function emitFrame(frame: string, handlers: ResearchStreamHandlers): boolean {
  const raw = readData(frame)
  if (raw === null) {
    return false
  }
  if (raw.trim() === '[DONE]') {
    return true
  }

  try {
    const event = JSON.parse(raw) as ResearchEvent
    if (!event || typeof event !== 'object' || Array.isArray(event)) {
      throw new Error('Stream event must be a JSON object')
    }
    handlers.onEvent(event)
  } catch (error) {
    handlers.onProtocolError(asError(error), raw)
  }
  return false
}

function takeFrames(buffer: string): { frames: string[]; remainder: string } {
  const normalized = buffer.replace(/\r\n/g, '\n')
  const parts = normalized.split('\n\n')
  return {
    frames: parts.slice(0, -1),
    remainder: parts[parts.length - 1] ?? '',
  }
}

export async function consumeResearchStream(
  stream: ReadableStream<Uint8Array>,
  handlers: ResearchStreamHandlers,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let doneReceived = false

  try {
    while (!doneReceived) {
      const result = await reader.read()
      if (result.done) {
        buffer += decoder.decode()
        break
      }

      buffer += decoder.decode(result.value, { stream: true })
      const { frames, remainder } = takeFrames(buffer)
      buffer = remainder
      for (const frame of frames) {
        if (emitFrame(frame, handlers)) {
          doneReceived = true
          break
        }
      }
    }

    if (!doneReceived && buffer.trim()) {
      emitFrame(buffer, handlers)
    }
  } catch (error) {
    handlers.onConnectionError(asError(error))
  } finally {
    reader.releaseLock()
  }
}
