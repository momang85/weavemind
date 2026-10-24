// P0-4：交付包口径的唯一来源（一个纯函数 + 一组固定文案）。
//
// 实机反例（2026-10-21 真机复验 README P0-4）：同一屏上「分析工作台」写
// 「当前包: deliverables_20261021_190912.zip（与采纳稿同版才可下载）」，
// 「导出与复核」却写「本次任务暂无交付包（打包步骤未产出 deliverables_*.zip）」。
// 两处各自读了一份状态：工作台读 /api/task/<id>/analysis 的 current_package；
// 导出面板读 report.export.package，而实时完成那条路径根本没带 export 字段。
// 这里把判定收敛成**一个纯函数**：两个面板调用同一函数、传同一组输入，输出必然一致；
// 「暂无交付包」只在这一个模块里有字面量，且只在两个来源都为空时返回。

export type PackageSource = 'export' | 'analysis' | 'none'

export interface PackageResolution {
  /** 解析出的包名；空串 = 两个来源都没有包 */
  name: string
  source: PackageSource
  hasPackage: boolean
  /** 后端给的包说明（analysis.package_note），没有则空串 */
  note: string
}

export interface PackageInputs {
  /** export.package（任务详情 payload） */
  exportPackage?: unknown
  /** analysis.current_package（分析工作台接口） */
  analysisPackage?: unknown
  /** analysis.package_note */
  packageNote?: unknown
}

const text = (v: unknown): string => (v == null ? '' : String(v).trim())

export function resolvePackage(input: PackageInputs): PackageResolution {
  const exp = text(input.exportPackage)
  const ana = text(input.analysisPackage)
  // 两个来源任一有值就算有包；同有值时以任务详情 payload 的 export.package 为准
  const name = exp || ana
  return {
    name,
    source: !name ? 'none' : (exp ? 'export' : 'analysis'),
    hasPackage: !!name,
    note: text(input.packageNote),
  }
}

/** 「暂无交付包」的**唯一**文案；只在 resolvePackage 判定 both-empty 时使用。 */
export const NO_PACKAGE_NOTE =
  '本次任务暂无交付包（打包步骤未产出 deliverables_*.zip）；当前按文件逐个下载——'
  + '上方"生成文件"列表与正文里的图表都带下载按钮。'

/** 包状态读不到时**不下结论**（没读到 ≠ 没有）。 */
export const PACKAGE_LOADING_NOTE = '正在核对交付包…（还没读到包名，先不下"有没有包"的结论）'

/** 导出与复核面板的说明：有包说有包，没包才说"暂无交付包"。 */
export function exportPanelNote(res: PackageResolution, generatedAt?: unknown): string {
  if (res.hasPackage) {
    const at = text(generatedAt) || '未知时间'
    return `交付包 ${res.name}（生成于 ${at}）按登录会话放行下载；`
      + '包可能早于最近一次修订，正文里的图表与"生成文件"列表始终按当前版本导出。'
      + '修订后用"按当前版本重新导出"生成新包（旧包保留不动）。'
  }
  return NO_PACKAGE_NOTE
}

/** 分析工作台底部一行：与导出面板共用同一解析结果（同一 resolvePackage 调用）。 */
export function packageLine(
  res: PackageResolution,
  opts: { adoptedIdentity?: unknown } = {},
): string {
  if (res.hasPackage) return `当前包：${res.name}（与采纳稿同版才可下载）`
  const ident = text(opts.adoptedIdentity)
  if (ident) return `尚无与当前采纳稿（${ident.slice(0, 12)}）同版的包：点"导出当前包"会先生成`
  if (res.note) return `交付包：${res.note}`
  return ''
}

/** 简报面板「导出」小节的一行：同样只吃 resolvePackage 的结果（不再有第三个判定）。 */
export function briefPackageLine(res: PackageResolution, generatedAt?: unknown): string {
  if (res.hasPackage) {
    return `交付包 ${res.name}（生成于 ${text(generatedAt) || '未知时间'}）`
  }
  return '暂无交付包：任务详情的 `export.package` 与分析工作台的 `current_package` '
    + '两个来源都没有包（打包步骤未产出 deliverables_*.zip），按上方文件逐个下载'
}
