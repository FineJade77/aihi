/** LSP-style Content-Length framing used by the local Python Worker. */

export const MAX_FRAME_BYTES = 4 * 1024 * 1024;
export const MAX_HEADER_BYTES = 16 * 1024;

export class FrameProtocolError extends Error {
  public readonly code = "FRAME_PROTOCOL_ERROR" as const;

  public constructor(message: string) {
    super(message);
    this.name = "FrameProtocolError";
  }
}

type JsonObject = Record<string, unknown>;

const UTF8_DECODER = new TextDecoder("utf-8", { fatal: true });

/**
 * Incrementally decodes Content-Length framed JSON objects.
 *
 * stdout is a byte stream, so a single data event may contain a partial frame,
 * multiple frames, or a frame boundary in the middle of a UTF-8 code point.
 * Keeping the buffer as bytes avoids corrupting multibyte payloads.
 */
export class ContentLengthDecoder {
  private buffer = Buffer.alloc(0);

  public constructor(
    private readonly maxFrameBytes = MAX_FRAME_BYTES,
    private readonly maxHeaderBytes = MAX_HEADER_BYTES,
  ) {}

  public push(chunk: Uint8Array): JsonObject[] {
    if (chunk.byteLength > 0) {
      this.buffer = Buffer.concat([this.buffer, Buffer.from(chunk)]);
    }
    const messages: JsonObject[] = [];

    while (true) {
      const headerEnd = this.findHeaderEnd();
      if (headerEnd < 0) {
        if (this.buffer.byteLength > this.maxHeaderBytes) {
          throw new FrameProtocolError("Frame header exceeds the configured limit");
        }
        break;
      }
      if (headerEnd > this.maxHeaderBytes) {
        throw new FrameProtocolError("Frame header exceeds the configured limit");
      }

      const header = this.buffer.subarray(0, headerEnd).toString("ascii");
      const bodyOffset = this.headerTerminatorLength(headerEnd);
      const contentLength = this.parseContentLength(header);
      if (contentLength > this.maxFrameBytes) {
        throw new FrameProtocolError("Frame body exceeds the configured limit");
      }
      const frameEnd = headerEnd + bodyOffset + contentLength;
      if (this.buffer.byteLength < frameEnd) {
        break;
      }

      const bodyBytes = this.buffer.subarray(headerEnd + bodyOffset, frameEnd);
      this.buffer = this.buffer.subarray(frameEnd);
      let decoded: unknown;
      try {
        decoded = JSON.parse(UTF8_DECODER.decode(bodyBytes));
      } catch (error) {
        throw new FrameProtocolError(
          `Frame body is not valid UTF-8 JSON: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
      if (decoded === null || typeof decoded !== "object" || Array.isArray(decoded)) {
        throw new FrameProtocolError("JSON-RPC frame must contain a JSON object");
      }
      messages.push(decoded as JsonObject);
    }

    return messages;
  }

  public get bufferedBytes(): number {
    return this.buffer.byteLength;
  }

  private findHeaderEnd(): number {
    const crlfEnd = this.buffer.indexOf(Buffer.from("\r\n\r\n"));
    const lfEnd = this.buffer.indexOf(Buffer.from("\n\n"));
    if (crlfEnd < 0) {
      return lfEnd;
    }
    if (lfEnd < 0) {
      return crlfEnd;
    }
    return Math.min(crlfEnd, lfEnd);
  }

  private headerTerminatorLength(headerEnd: number): number {
    return this.buffer.subarray(headerEnd, headerEnd + 4).toString("ascii") === "\r\n\r\n"
      ? 4
      : 2;
  }

  private parseContentLength(header: string): number {
    let value: string | undefined;
    for (const line of header.split(/\r?\n/)) {
      if (!line.trim()) {
        continue;
      }
      const separator = line.indexOf(":");
      if (separator < 0) {
        throw new FrameProtocolError("Malformed frame header");
      }
      const name = line.slice(0, separator).trim().toLowerCase();
      if (name !== "content-length") {
        continue;
      }
      if (value !== undefined) {
        throw new FrameProtocolError("Duplicate Content-Length header");
      }
      value = line.slice(separator + 1).trim();
    }
    if (value === undefined || !/^\d+$/.test(value)) {
      throw new FrameProtocolError("Content-Length header is required and must be an integer");
    }
    const contentLength = Number(value);
    if (!Number.isSafeInteger(contentLength) || contentLength < 0) {
      throw new FrameProtocolError("Content-Length header is outside the supported range");
    }
    return contentLength;
  }
}

export function encodeFrame(
  payload: JsonObject,
  maxFrameBytes = MAX_FRAME_BYTES,
): Buffer {
  let body: string;
  try {
    const serialized = JSON.stringify(payload);
    if (serialized === undefined) {
      throw new TypeError("JSON.stringify returned undefined");
    }
    body = serialized;
  } catch (error) {
    throw new FrameProtocolError(
      `Frame payload cannot be serialized: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const bodyBytes = Buffer.from(body, "utf8");
  if (bodyBytes.byteLength > maxFrameBytes) {
    throw new FrameProtocolError("Frame body exceeds the configured limit");
  }
  return Buffer.concat([
    Buffer.from(`Content-Length: ${bodyBytes.byteLength}\r\n\r\n`, "ascii"),
    bodyBytes,
  ]);
}
