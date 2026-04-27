/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Manrope', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        ink: {
          950: '#070912',
          900: '#0c1020',
          800: '#141a2e',
          700: '#1c233c',
          600: '#2a3354',
        },
      },
      boxShadow: {
        'glow-blue': '0 8px 24px -8px rgba(96, 165, 250, 0.45)',
        'glow-violet': '0 8px 24px -8px rgba(167, 139, 250, 0.45)',
        'glow-rose': '0 8px 24px -8px rgba(251, 113, 133, 0.45)',
        'glow-amber': '0 8px 24px -8px rgba(251, 191, 36, 0.45)',
        'glow-emerald': '0 8px 24px -8px rgba(52, 211, 153, 0.45)',
        'inner-soft': 'inset 0 1px 0 0 rgba(255, 255, 255, 0.06)',
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