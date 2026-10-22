// N3：浏览器内首次使用引导——新人只填无法代办的信息，不编辑配置、不进终端。
//
// 五步（可折叠，默认只展开当前该做的一步）：
//   ① 先体验或配置：无密钥也能看内置演示（页面**一直标注**"非本次实时生成"）
//   ② 一次配置：服务/密钥/模型（高级地址与多模型默认折叠）；连通性测试由用户点击触发
//   ③ 账户与数据位置：管理员已创建/如何创建；数据放在哪、密钥不进日志
//   ④ 首个研究任务：示例目标可直接填入（不自动提交）
//   ⑤ 教读者读结果：机器验收 / 问题覆盖 / 人工复核三态，以及修订与同版下载
//
// 纪律：演示是合成数据，不写入任何研究统计；密钥只走既有 /api/config（不回显、不入
// URL/日志）；不自动扫描模型、不自动提交任务。

import { useEffect, useState } from 'react'
import { Compass, BookOpen, KeyRound, PlayCircle, ClipboardList, ChevronDown, ChevronRight } from 'lucide-react'

type Props = {
  configComplete: boolean
  demoAvailable: boolean
  /** 代码执行隔离状态（只读布尔 + 既有说明文案；提交任务前就要能看到） */
  codeExecution?: { isolation_ready: boolean; isolation_required: boolean; execution_available: boolean; note: string } | null
  /** 把示例目标填进提交框（不提交） */
  onPrefillGoal?: (goal: string) => void
  /** 引导里"去配置"滚动到设置区 */
  onGoSettings?: () => void
}

const SAMPLE_GOAL =
  '研究洋河股份（002304.SZ）2023—2024 年度经营表现。使用合并报表口径，' +
  '资料截止日 2025-04-30，读者为独立研究员。\n' +
  '重点回答：\n' +
  '1. 收入变化能从销量、价格或结构解释到哪一步？\n' +
  '2. 利润变化有哪些直接披露或可复算依据？\n' +
  '3. 经营现金流变化还需要哪些附注才能解释？\n' +
  '将数据观察、发行人说法、计算结果与待核查判断分开；缺证据时明确保留缺口。'

const box = 'bg-surface border border-line rounded-xl px-4 py-3'
const li = 'text-xs text-ink-muted leading-relaxed'

export default function FirstRunGuide({ configComplete, demoAvailable, codeExecution, onPrefillGoal, onGoSettings }: Props) {
  const [open, setOpen] = useState<string | null>(configComplete ? null : 'start')
  const [demo, setDemo] = useState<{ markdown: string; note: string } | null>(null)
  const [demoError, setDemoError] = useState('')
  const [guideOpen, setGuideOpen] = useState(!configComplete)
  useEffect(() => {
    setOpen(configComplete ? null : 'start')
    setGuideOpen(!configComplete)
  }, [configComplete])

  const loadDemo = async () => {
    setDemoError('')
    try {
      const res = await fetch('/api/demo/brief')
      const d = await res.json()
      if (!res.ok || !d?.available) { setDemoError(d?.note || '内置演示不可用'); return }
      setDemo({ markdown: String(d.markdown || ''), note: String(d.note || '') })
    } catch {
      setDemoError('读取内置演示失败')
    }
  }

  const Step = ({ id, icon: Icon, title, children }: {
    id: string; icon: typeof Compass; title: string; children: React.ReactNode
  }) => (
    <div className="border-t border-line first:border-t-0 pt-3 first:pt-0">
      <button type="button" onClick={() => setOpen(open === id ? null : id)}
        className="flex w-full items-center gap-2 text-left">
        {open === id ? <ChevronDown className="h-3.5 w-3.5 text-ink-faint" />
          : <ChevronRight className="h-3.5 w-3.5 text-ink-faint" />}
        <Icon className="h-3.5 w-3.5 text-brand" />
        <span className="text-xs font-medium text-ink">{title}</span>
      </button>
      {open === id && <div className="mt-2 space-y-2 pl-6">{children}</div>}
    </div>
  )

  return (
    <div className={`${box} space-y-3`}>
      <button type="button" onClick={() => setGuideOpen(!guideOpen)} aria-expanded={guideOpen} className="flex w-full flex-wrap items-center gap-2 text-left">
        <Compass className="h-4 w-4 text-brand" />
        <span className="text-sm text-ink">使用指南</span>
        <span className="hidden text-xs text-ink-faint sm:inline">
          {configComplete ? '模型已配置，可直接提交研究任务' : '先把模型配好，或用内置演示先看交付形态'}
        </span>
        <ChevronDown className={`ml-auto h-4 w-4 text-ink-faint ${guideOpen ? 'rotate-180' : ''}`} />
      </button>

      {/* N4 场景 8：代码执行能力在**提交任务之前**说明白——否则用户要等任务跑完，
          才从失败步骤里发现含代码的步骤根本执行不了。不提供"关闭隔离"这条出路。 */}
      {codeExecution && codeExecution.isolation_required && !codeExecution.isolation_ready && (
        <div data-code-execution-notice
          className="rounded-lg border border-line-strong bg-surface-sunken/60 px-3 py-2 text-xs text-ink-muted">
          <span>高级代码执行暂不可用；公司研究、图表、报告与下载<span className="text-ink">不受影响</span>。</span>
          <details className="mt-1"><summary>查看原因与安全说明</summary>
            <p className="mt-1 break-words">本机没有可用的容器隔离，代码步骤将被拒绝，不会退到本机运行。请不要用"关闭隔离"来解决。{codeExecution.note}</p>
          </details>
        </div>
      )}

      {guideOpen && <div className="space-y-3 border-t border-line pt-3"><Step id="start" icon={PlayCircle} title="① 先体验，或先配置">
        <p className={li}>
          没有密钥也能先看一份**内置演示**（合成数据），或者直接配置你自己的模型服务。
          真实研究必须完成自己的模型配置——演示不会代替它。
        </p>
        <div className="flex flex-wrap gap-2">
          {demoAvailable && (
            <button type="button" onClick={loadDemo}
              className="rounded-lg bg-surface-raised px-3 py-1.5 text-xs text-ink hover:bg-line">
              查看内置演示（非实时生成）
            </button>
          )}
          <button type="button" onClick={() => { setOpen('config'); onGoSettings?.() }}
            className="rounded-lg bg-brand/10 px-3 py-1.5 text-xs text-brand hover:bg-brand/20">
            去配置模型服务
          </button>
        </div>
        {demoError && <p className="text-xs text-amber-700">{demoError}</p>}
        {demo && (
          <div className="space-y-2">
            <div data-demo-banner
              className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-700">
              {demo.note || '内置演示：非本次实时生成，不代表真实研究结果；不写入任何研究统计'}
            </div>
            <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-sunken/60 p-3 text-xs text-ink">
              {demo.markdown}
            </pre>
          </div>
        )}
      </Step>

      <Step id="config" icon={KeyRound} title="② 一次配置（服务 / 密钥 / 模型）">
        <p className={li}>
          在「设置 → 模型」里选服务商、填密钥、选模型即可；地址与多模型角色属高级项，
          默认折叠。连通性测试**由你点击触发**（会产生极少量请求，可能计费），系统不会自动扫描模型。
        </p>
        <p className={li}>密钥只存在本地配置里：不回显、不入 URL 与日志。</p>
      </Step>

      <Step id="account" icon={ClipboardList} title="③ 账户与数据位置">
        <p className={li}>
          首个管理员通过页面创建（没有默认密码）；创建后写入入口关闭。
          研究数据保存在本机工作区与数据库里，模型、检索与数据接口会收到相应请求——
          **本地部署不等于离线运行**。
        </p>
      </Step>

      <Step id="task" icon={PlayCircle} title="④ 第一个研究任务">
        <p className={li}>
          用「研究一家公司」表单填公司、期间、口径与资料截止日，或直接改下面这段示例。
          默认公司研究**不需要**代码沙箱。
        </p>
        <button type="button"
          onClick={() => onPrefillGoal?.(SAMPLE_GOAL)}
          className="rounded-lg bg-surface-raised px-3 py-1.5 text-xs text-ink hover:bg-line">
          把示例目标填进提交框（不自动提交）
        </button>
      </Step>

      <Step id="read" icon={BookOpen} title="⑤ 怎么读结果">
        <ul className={li}>
          <li>· **机器验收**：数字、来源声明与交付完整性的规则检查——不等于结论正确。</li>
          <li>· **问题覆盖**：必答问题是否被材料回答；部分覆盖与"无相关材料"都会如实标出。</li>
          <li>· **人工复核**：是否有人审阅过**这一版**；计划评审通过不等于人工复核。</li>
        </ul>
        <p className={li}>
          先看「关键判断与下一步」与『逐问题资料计划』（缺什么、补到后判断怎么变），
          再决定：改正文 → 重验 → 用「按当前版本重新导出」拿同版交付包。
          资料不足时按缺口清单补材料，而不是反复重跑。
        </p>
      </Step>
      </div>}
    </div>
  )
}
