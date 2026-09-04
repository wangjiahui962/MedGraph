// 与本地后端（server.py，经 Vite 代理 /api）交互的封装。

export interface Job {
  id: string;
  kind: "collect" | "extract";
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  message: string;
  detail: string | null;
}

/** 请求超时时间：后端接口本身都会快速返回，只有任务在后台线程执行。 */
const REQUEST_TIMEOUT_MS = 20_000;

/** 探测后端是否存活（短超时）。Vite 代理在后端未启动时返回 HTTP 500，用此区分原因。 */
async function backendReachable(): Promise<boolean> {
  try {
    const probe = new AbortController();
    const probeTimer = setTimeout(() => probe.abort(), 2500);
    const res = await fetch("/api/health", { signal: probe.signal });
    clearTimeout(probeTimer);
    return res.ok;
  } catch {
    return false;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(path, { ...init, signal: controller.signal });
  } catch (e) {
    clearTimeout(timer);
    if (controller.signal.aborted) {
      throw new Error("请求超时，请确认后端服务（python server.py）已启动。");
    }
    if (!(await backendReachable())) {
      throw new Error("无法连接本地后端，请先运行 python server.py（默认端口 8756）。");
    }
    throw e;
  }
  clearTimeout(timer);
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).error || "";
    } catch {
      /* 忽略解析失败（Vite 代理的后端 500 响应不是 JSON） */
    }
    if (!detail && response.status >= 500 && !(await backendReachable())) {
      throw new Error("后端服务未启动（HTTP 500）：请先运行 python server.py（默认端口 8756）后重试。");
    }
    throw new Error(`后端请求失败（HTTP ${response.status}）${detail}`.trim());
  }
  return (await response.json()) as T;
}

/** 启动“提取现有数据”任务。limit>0 时只抽取前 N 篇。 */
export function startExtract(limit = 0): Promise<{ job_id: string }> {
  return request("/api/extract", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit }),
  });
}

/** 启动“增加新数据”任务：采集后默认自动完成抽取并更新图谱。 */
export function startCollect(
  count = 5,
  autoExtract = true,
): Promise<{ job_id: string }> {
  return request("/api/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count, auto_extract: autoExtract }),
  });
}

/** 查询最近任务列表。 */
export function fetchJobs(): Promise<{ jobs: Job[] }> {
  return request("/api/jobs");
}
