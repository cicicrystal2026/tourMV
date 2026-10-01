/** PRD §10.3 设计规范落地为 Tailwind token */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: '#0F5E57',
          dark: '#0A423D',
          mid: '#1D9E75',
          soft: '#E8F1EF',
          line: '#BFD8D3',
        },
        human: {
          DEFAULT: '#C2611F',
          soft: '#FBEBD9',
          dark: '#8C4413',
        },
        sidebar: '#1C1B19',
        page: '#F6F4EF',
        line: '#E3DFD6',
        ink: { DEFAULT: '#1C1B19', soft: '#5F5E5A', faint: '#888780' },
      },
      fontFamily: {
        serif: ['"Noto Serif SC"', 'Songti SC', 'SimSun', 'serif'],
        sans: ['"Noto Sans SC"', '"Microsoft YaHei"', 'system-ui', 'sans-serif'],
      },
      borderRadius: { card: '12px', btn: '8px' },
      minHeight: { btn: '36px' },
      height: { btn: '36px', btnlg: '44px' },
      maxWidth: { screen: '1440px' },
    },
  },
  plugins: [],
}
