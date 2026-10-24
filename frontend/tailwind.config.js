export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: { DEFAULT: '#ffffff', raised: '#f3f1eb', sunken: '#f8f7f3' },
        line: { DEFAULT: '#e8e5dd', strong: '#cec9bc' },
        ink: { DEFAULT: '#292d2b', muted: '#626760', faint: '#6c7068' },
        brand: { DEFAULT: '#876728', soft: '#f7f1e4', strong: '#70531f' },
        state: {
          running: '#876728', success: '#27715a', issue: '#936719',
          failure: '#b14539', idle: '#6c7068'
        }
      },
      // 字号下限：12px（text-xs）。守卫测试禁止新增 text-[<12px] 任意值。
      fontSize: { '2xs': '12px' }
    }
  },
  plugins: []
}
