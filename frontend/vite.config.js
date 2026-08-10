import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: process.env.PORT ? parseInt(process.env.PORT) : 5173,
    proxy: {
      // Nginx now terminates TLS on 443; port 80 only issues a redirect.
      // secure: false accepts the self-signed dev certificate.
      '/shorten': { target: 'https://localhost:443', changeOrigin: true, secure: false },
    },
  },
});
