/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        primary: {
          light: '#60A5FA',
          DEFAULT: '#A78BFA',
          pink: '#F472B6',
        },
        bg: {
          deep: '#1E1B4B',
          mid: '#312E81',
          blue: '#1E3A8A',
        },
        ink: {
          DEFAULT: '#F1F5F9',
          muted: '#C7D2FE',
        },
        ok: '#6EE7B7',
        warn: '#FDE68A',
        err: '#FCA5A5',
        cyan: '#00F2FE',
        green: '#34D399',
      },
      fontFamily: {
        sans: ['PingFang SC', 'Microsoft YaHei', 'system-ui', 'sans-serif'],
      },
      boxShadow: {
        glow: '0 6px 20px rgba(0, 0, 0, 0.18)',
        'glow-lg': '0 12px 32px rgba(0, 0, 0, 0.28)',
      },
    },
  },
  plugins: [require('tailwindcss-animate')],
}
