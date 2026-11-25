/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './sova/dashboard/templates/**/*.html',
    './sova/dashboard/static/**/*.js',
  ],
  // Words in comments and JS strings ('resize', "ease-in"/"ease-out" for the motion tokens) are picked up as class
  // names. None is used as a class, and Tailwind's ease-out curve is not the design token --ease-out, so a future
  // class="ease-out" must not quietly get the wrong curve: use the tokens in style.css instead.
  blocklist: ['resize', 'ease-in', 'ease-out'],
  theme: {
    extend: {
      colors: {
        sidebar: {
          DEFAULT: 'rgb(var(--ctp-base-rgb) / <alpha-value>)',
          hover:   'rgb(var(--ctp-surface0-rgb) / <alpha-value>)',
          active:  'rgb(var(--ctp-surface1-rgb) / <alpha-value>)',
        },
        surface: {
          DEFAULT: 'rgb(var(--ctp-mantle-rgb) / <alpha-value>)',
          card:    'rgb(var(--ctp-base-rgb) / <alpha-value>)',
          hover:   'rgb(var(--ctp-surface0-rgb) / <alpha-value>)',
        },
        accent: {
          DEFAULT:  'rgb(var(--ctp-blue-rgb) / <alpha-value>)',
          green:    'rgb(var(--ctp-green-rgb) / <alpha-value>)',
          red:      'rgb(var(--ctp-red-rgb) / <alpha-value>)',
          yellow:   'rgb(var(--ctp-yellow-rgb) / <alpha-value>)',
          purple:   'rgb(var(--ctp-mauve-rgb) / <alpha-value>)',
          peach:    'rgb(var(--ctp-peach-rgb) / <alpha-value>)',
          lavender: 'rgb(var(--ctp-lavender-rgb) / <alpha-value>)',
          teal:     'rgb(var(--ctp-teal-rgb) / <alpha-value>)',
        },
      }
    }
  }
}
