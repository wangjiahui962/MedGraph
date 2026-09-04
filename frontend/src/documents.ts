// 按需加载文档全文：数据来自 frontend/public/data/documents.json
// （由 python db/store_documents.py --export-frontend 生成、predev/prebuild 同步）。
// 仅在用户第一次点击证据的“展开”时才发起一次请求，之后结果缓存在内存中。

export interface DocumentRecord {
  document_id: string;
  title: string;
  content: string;
}

let pending: Promise<Map<string, DocumentRecord>> | null = null;

function loadDocuments(): Promise<Map<string, DocumentRecord>> {
  pending ??= (async () => {
    const response = await fetch(
      `${import.meta.env.BASE_URL}data/documents.json`,
    );
    if (!response.ok) {
      throw new Error(
        `文档数据加载失败（HTTP ${response.status}），请确认已同步 documents.json。`,
      );
    }
    const rows = (await response.json()) as DocumentRecord[];
    const records = new Map<string, DocumentRecord>();
    for (const row of rows) {
      if (row && typeof row.document_id === "string" && row.document_id) {
        records.set(row.document_id, row);
      }
    }
    return records;
  })();
  return pending;
}

/** 按文档编号取文档全文；文档库中不存在时返回 undefined。 */
export async function fetchDocument(
  documentId: string,
): Promise<DocumentRecord | undefined> {
  const records = await loadDocuments();
  return records.get(documentId);
}