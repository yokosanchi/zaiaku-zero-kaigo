import { defineCollection, z } from "astro:content";

const blog = defineCollection({
  type: "content",
  schema: z.object({
    title: z.string(),
    description: z.string(),
    pubDate: z.coerce.date(),
    keyword: z.string(),
    category: z.string(),
    target_searcher: z.string(),
    conversion_type: z.string(),
    draft: z.boolean().optional().default(false),
  }),
});

export const collections = { blog };
