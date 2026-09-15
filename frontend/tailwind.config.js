export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // 语义色令牌：新代码（从 components/ui.tsx 起）用语义名而非散写 slate-*，
        // 取值与原 slate 色一致，迁移期两者共存。守卫测试校验"声明的令牌都有使用点"，
        // 原先的 accent 就是这样被查出零引用后删除的。
        surface: { DEFAULT: '#0f172a', raised: '#1e293b', sunken: '#020617' },
        line: { DEFAULT: '#1e293b', strong: '#334155' },
        ink: { DEFAULT: '#e2e8f0', muted: '#94a3b8', faint: '#64748b' },
        state: {
          running: '#22d3ee', success: '#34d399', issue: '#fbbf24',
          failure: '#f87171', idle: '#64748b'
        }
      },
      // 字号下限：12px（text-xs）。守卫测试禁止新增 text-[<12px] 任意值。
      fontSize: { '2xs': '12px' }
    }
  },
  plugins: []
}
