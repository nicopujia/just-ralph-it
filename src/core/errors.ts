export class JriError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly recovery: string,
  ) {
    super(message);
    this.name = "JriError";
  }
}

export function isJriError(error: unknown): error is JriError {
  return error instanceof JriError;
}
