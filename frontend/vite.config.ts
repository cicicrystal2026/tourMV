import { defineConfig } from 'vite'

// 说明：常规做法是引入 @vitejs/plugin-react 以获得 Fast Refresh。
// 这里刻意不依赖它——Vite 本身用 esbuild 就能转译 TSX（配合 tsconfig 的
// "jsx": "react-jsx"，走自动 JSX runtime）。少一个插件就少一整棵 @babel 依赖树，
// 在受限网络下更容易装起来；代价是改代码时是整页刷新而非局部热更新。
export default defineConfig({
  esbuild: { jsx: 'automatic' },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/media': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
