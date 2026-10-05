import sharp from "sharp";

await sharp("architecture.svg", { density: 72 })
  .jpeg({ quality: 92, chromaSubsampling: "4:4:4" })
  .toFile("architecture.jpg");
