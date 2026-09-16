FROM node:24-alpine

WORKDIR /app
COPY package.json index.js ./
USER node
EXPOSE 23210
CMD ["node", "index.js", "--host", "0.0.0.0"]
