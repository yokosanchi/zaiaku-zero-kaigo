import type { APIRoute } from "astro";
import { getCollection } from "astro:content";

const SITE_URL = "https://zaiaku-zero-kaigo.yokosanchi.workers.dev";

export const GET: APIRoute = async () => {
  const posts = await getCollection("blog", ({ data }) => !data.draft);
  const urls = [
    `${SITE_URL}/`,
    ...posts.map((post) => `${SITE_URL}/qa/${post.slug}/`),
  ];

  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls.map((url) => `  <url><loc>${url}</loc></url>`).join("\n")}
</urlset>
`;

  return new Response(body, {
    headers: { "Content-Type": "application/xml; charset=utf-8" },
  });
};
