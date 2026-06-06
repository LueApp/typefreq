import { defineConfig } from 'astro/config';

import cloudflare from '@astrojs/cloudflare';

export default defineConfig({
  site: process.env.SITE_URL ?? 'https://keyfreq.lue-app.com',
  output: 'static',

  prefetch: {
    prefetchAll: true,
    defaultStrategy: 'viewport',
  },

  adapter: cloudflare(),
});