import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 相对路径 base：产物可以被丢到任意静态托管的任意子目录下直接打开。
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom'],
        },
      },
    },
  },
});
