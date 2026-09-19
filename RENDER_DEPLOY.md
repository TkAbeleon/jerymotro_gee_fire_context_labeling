# Déploiement Render

Architecture :

PC -> WSS -> Render -> TCP 5432 -> Layerbase PostgreSQL

Le service Render utilise :
- `requirements-render.txt`
- `uvicorn pg_websocket_gateway:app --host 0.0.0.0 --port $PORT`
- `/health` pour le health check
- `/wss` pour le WebSocket

## Déploiement

1. Sur Render, créer un Web Service depuis ce dépôt, ou utiliser le Blueprint `render.yaml`.
2. Laisser Render générer `PG_GATEWAY_TOKEN`.
3. Vérifier que `PG_REMOTE_HOST` et `PG_REMOTE_PORT` sont présents.
4. Après le déploiement, vérifier :
   `https://<service>.onrender.com/health`
5. Le client local utilise :
   `wss://<service>.onrender.com/wss`

Sur le PC :

```bash
export PG_WS_URL='wss://<service>.onrender.com/wss'
export PG_GATEWAY_TOKEN='LE_TOKEN_RENDER'
python3 pg_websocket_client.py
```

Puis :

```bash
psql "postgresql://postgres:PASSWORD@127.0.0.1:15432/jerymotro?sslmode=require"
```

Ne jamais commiter le token ou le mot de passe PostgreSQL.

Le plan Free peut mettre le service en veille après une période sans trafic ;
la première requête peut donc nécessiter le réveil du service.
