/** @type {import('tailwindcss').Config} */
// AutoTeam Bright v1 — round-12 F1
// 深色玻璃风 → 明亮 dashboard
// 调研：.trellis/tasks/05-11-upstream-align-register-multimail-frontend-refresh/research/frontend-bright-icon.md
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Manrope', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        // 中性灰阶（保留 ink 命名，所有引用 ink-* 的组件无须改）
        ink: {
          50: '#fafafa',
          100: '#f5f5f5',
          200: '#e5e5e5',
          300: '#d4d4d4',
          400: '#a3a3a3',
          500: '#737373',
          600: '#525252',
          700: '#404040',
          800: '#262626',
          900: '#171717',
          950: '#0a0a0a',
        },
        // 语义 token
        canvas: '#fafafa',
        surface: { DEFAULT: '#ffffff', hover: '#f5f5f5' },
        hairline: { DEFAULT: '#e5e5e5', strong: '#d4d4d4' },
      },
      boxShadow: {
        // Linear/Vercel 风轻盈 elevation
        'card': '0 1px 2px 0 rgba(0,0,0,0.04), 0 1px 3px 0 rgba(0,0,0,0.06)',
        'card-hover': '0 4px 12px -2px rgba(0,0,0,0.08), 0 2px 4px -2px rgba(0,0,0,0.04)',
        'ring-accent': '0 0 0 3px rgba(79, 70, 229, 0.15)',
        // 兼容旧 glow-* 命名 → 改为微弱 elevation，避免组件硬引用炸（视觉显著弱化是 by design）
        'glow-blue': '0 6px 16px -8px rgba(79, 70, 229, 0.25)',
        'glow-violet': '0 6px 16px -8px rgba(124, 58, 237, 0.25)',
        'glow-rose': '0 6px 16px -8px rgba(225, 29, 72, 0.20)',
        'glow-amber': '0 6px 16px -8px rgba(217, 119, 6, 0.20)',
        'glow-emerald': '0 6px 16px -8px rgba(5, 150, 105, 0.20)',
        'inner-soft': 'inset 0 1px 0 0 rgba(0, 0, 0, 0.03)',
      },
      ringColor: {
        DEFAULT: '#4f46e5',
      },
      animation: {
        'pulse-dot': 'pulseDot 1.8s ease-in-out infinite',
        'shimmer': 'shimmer 2.4s ease-in-out infinite',
        'toast-in': 'toastIn 220ms cubic-bezier(0.22, 1, 0.36, 1)',
        'toast-out': 'toastOut 180ms ease-in forwards',
        'rise': 'rise 360ms cubic-bezier(0.22, 1, 0.36, 1)',
      },
      keyframes: {
        pulseDot: {
          '0%, 100%': { transform: 'scale(1)', opacity: '1' },
          '50%': { transform: 'scale(1.6)', opacity: '0.4' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        toastIn: {
          '0%': { transform: 'translateY(8px) scale(0.96)', opacity: '0' },
          '100%': { transform: 'translateY(0) scale(1)', opacity: '1' },
        },
        toastOut: {
          '0%': { transform: 'translateY(0)', opacity: '1' },
          '100%': { transform: 'translateY(-6px)', opacity: '0' },
        },
        rise: {
          '0%': { transform: 'translateY(6px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
      },
    },
  },
  plugins: [],
}
