import { copyFile, mkdir } from "node:fs/promises";

const target = new URL("../public/data/", import.meta.url);
const triplesSource = new URL("../../data/processed/triples.json", import.meta.url);
const documentsSource = new URL("../../data/processed/documents.json", import.meta.url);

try {
  await mkdir(target, { recursive: true });
  await copyFile(triplesSource, new URL("triples.json", target));
  console.log("已同步现有三元组 → public/data/triples.json");
} catch (error) {
  console.error(
    "无法同步三元组数据，请确认项目 data/processed/triples.json 存在。",
    error.message,
  );
  process.exitCode = 1;
}

try {
  await copyFile(documentsSource, new URL("documents.json", target));
  console.log("已同步文档全文 → public/data/documents.json");
} catch {
  // 文档全文可选：未生成时证据面板的“展开”会提示缺少原文，不影响图谱浏览
  console.warn(
    "未找到 data/processed/documents.json，跳过文档全文同步。",
    "（可运行 python db/store_documents.py --export-frontend 生成）",
  );
}
