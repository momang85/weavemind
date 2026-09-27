/** C3：把"可行动状态"的动作翻译成**页面意图**（纯函数，便于离线行为测试）。
 *
 * 后端 `actionable_state` 给的是动作名（`add_material`/`open_settings`/`retry`…），
 * 页面要把它变成具体的事：切到哪个标签、跳哪个路由、还是重新提交。
 * 这一层单独放纯函数，是为了让"该给哪个入口"可测——而不是埋在 JSX 里。
 */

export type ActionableIntent =
  | { kind: 'switch-tab'; tab: 'materials' | 'results' }
  | { kind: 'navigate'; to: string }
  | { kind: 'retry' }
  | { kind: 'none' }

/** 动作名 → 页面意图。认不出的动作一律 `none`（不给用户一个没用的按钮）。 */
export function actionIntent(action?: string): ActionableIntent {
  switch (String(action || '')) {
    case 'add_material':
      return { kind: 'switch-tab', tab: 'materials' }
    case 'view_details':
      return { kind: 'switch-tab', tab: 'results' }
    case 'open_settings':
      return { kind: 'navigate', to: '/settings' }
    case 'open_health':
      return { kind: 'navigate', to: '/health' }
    case 'retry':
      return { kind: 'retry' }
    default:
      // wait / none / 未知：等就行，不给按钮
      return { kind: 'none' }
  }
}

/** 是否需要把这条提示显示出来：只有"有话说"的状态才显示，执行中/已完成不打扰。 */
export function shouldShowActionable(a?: { state?: string; message?: string } | null): boolean {
  const state = String(a?.state || '')
  if (!state || !String(a?.message || '').trim()) return false
  // 执行中只显示一行状态，不占版面去讲"该做什么"（那时用户什么也不用做）
  return state !== 'running' && state !== 'done'
}

/** 状态 → 徽标配色（未知/待消费等中性状态不得显示成绿色）。 */
export function actionableTone(state?: string): 'ok' | 'warn' | 'bad' | 'idle' {
  switch (String(state || '')) {
    case 'done':
      return 'ok'
    case 'waiting_material':
    case 'pending_consume':
      return 'warn'
    case 'failed':
      return 'bad'
    default:
      return 'idle'
  }
}
