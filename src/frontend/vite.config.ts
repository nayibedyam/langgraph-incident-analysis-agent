import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const backendTarget = env.VITE_API_TARGET || 'http://127.0.0.1:8800';

  return {
    plugins: [react()],
    resolve: {
      alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
    },
    server: {
      port: 3100,
      host: true,
      // Allow any Host header — required when this dev server is reached from
      // a reviewer machine via its fully-qualified domain name
      // (Vite 5.4 introduced a strict default-deny list).
      allowedHosts: true,
      proxy: {
        '/api': { target: backendTarget, changeOrigin: true },
      },
    },
    preview: { port: 3100, host: true, allowedHosts: true },
    build: { outDir: 'dist', sourcemap: true },
  };
});
