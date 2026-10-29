// Bound HTTP before Pi's parser/event queue: SSE comments and provider metadata emit no text deltas.
export const MAX_HTTP_BYTES = 16 * 1024 * 1024;

export function boundedFetch({ onResponse, onLimit, onRequest, onResponseBytes, fetchImpl = globalThis.fetch }) {
  return async (url, options) => {
    const body = options?.body;
    if (body != null && typeof body !== "string") throw new Error("unsupported_provider_request_body");
    const requestBytes = body ? Buffer.byteLength(body) : 0;
    if (requestBytes > MAX_HTTP_BYTES) {
      onLimit("context_too_large");
      throw new Error("context_too_large");
    }
    onRequest?.(requestBytes);
    const response = await fetchImpl(url, options);
    // Observe failures too: Pi's success hook is not called for SDK-thrown HTTP errors.
    await onResponse(response);
    if (!response.body) return response;
    let bytes = 0;
    const boundedBody = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        bytes += chunk.byteLength;
        onResponseBytes?.(chunk.byteLength);
        if (bytes > MAX_HTTP_BYTES) {
          onLimit("response_too_large");
          throw new Error("response_too_large");
        }
        controller.enqueue(chunk);
      },
    }));
    return new Response(boundedBody, {
      status: response.status, statusText: response.statusText, headers: response.headers,
    });
  };
}
