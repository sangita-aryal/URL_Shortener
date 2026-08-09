import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy API calls to the FastAPI backend during local development.
    // In production, Nginx routes /shorten and /{short_code} directly.
    proxy: {
      '/shorten': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
});
