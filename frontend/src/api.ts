/**
 * 页面共用的 HTTP 边界：成功返回接口数据，失败抛出可展示的错误。
 * T 描述后端响应契约；业务状态的更新和忙碌提示由调用方负责。
 */
export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly detail: unknown) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  // 上传文件时由浏览器生成 multipart boundary，不能手工指定 JSON 类型。
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    // 保留结构化detail供页面恢复失败任务，message只承载用户可读说明。
    const detail = payload?.detail ?? payload?.message ?? `请求失败（${response.status}）`;
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail.map((item) => item.msg || JSON.stringify(item)).join('；')
        : typeof detail?.message === 'string'
          ? detail.message
          : JSON.stringify(detail);
    throw new ApiError(message, response.status, detail);
  }
  return payload as T;
}

/** 当前写接口共用 JSON 编码；文件上传直接调用 api 并传入 FormData。 */
export function send<T>(path: string, body: unknown, method: 'POST' | 'PATCH' = 'POST'): Promise<T> {
  return api<T>(path, { method, body: JSON.stringify(body) });
}
