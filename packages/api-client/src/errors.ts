import type { ErrorBody } from '@ai3d/shared-types';

/** Every failed request becomes an ApiError with the backend's error code. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(status: number, body: Partial<ErrorBody> & { message: string }) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code ?? 'http_error';
    this.details = (body.details as Record<string, unknown> | undefined) ?? {};
    this.requestId = body.request_id ?? null;
  }

  /** The backend could not be reached at all (not running, wrong URL, offline). */
  get isNetworkError(): boolean {
    return this.status === 0;
  }
}

export function networkError(cause: unknown): ApiError {
  const error = new ApiError(0, {
    code: 'network_error',
    message: 'Cannot reach the AI 3D Studio backend. Is it running?',
  });
  (error as { cause?: unknown }).cause = cause;
  return error;
}

export function parseErrorBody(status: number, text: string): ApiError {
  try {
    const parsed = JSON.parse(text) as { error?: ErrorBody };
    if (parsed.error?.message) return new ApiError(status, parsed.error);
  } catch {
    // Not our JSON error shape (proxy error page, etc.)
  }
  return new ApiError(status, { message: `Request failed with status ${status}.` });
}
