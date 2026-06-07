import { defineConfig, sessionDrivers } from 'astro/config';

import cloudflare from '@astrojs/cloudflare';

export default defineConfig({
  site: process.env.SITE_URL ?? 'https://typefreq.lue-app.com',
  output: 'static',
  session: {
    driver: sessionDrivers.lruCache(),
  },

  prefetch: {
    prefetchAll: true,
    defaultStrategy: 'viewport',
  },

  adapter: cloudflare({
    imageService: 'passthrough',
  }),
});
