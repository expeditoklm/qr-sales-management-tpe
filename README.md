# QuickSellPay — Backend API v5.0

Backend FastAPI multi-tenant pour l'application mobile **QuickSellPay** (caisse enregistreuse avec QR codes, fiscalité MECeF, gestion de stock).

**Nouveautés v5 :** MySQL mono-base multi-tenant · Redis rate limiting · Token blacklist (révocation JWT) · Sentry monitoring · S3/R2 robuste · Script de migration SQLite→MySQL

---

## Stack technique

| Composant | Technologie |
|-----------|-------------|
| Framework | FastAPI 0.115 + Python 3.12 |
| Auth | JWT (access + refresh) + token blacklist |
| Base de données | **MySQL** mono-base multi-tenant (fallback SQLite dev) |
| Rate limiting | **Redis** sliding window (fallback in-memory) |
| Stockage fichiers | **S3 / Cloudflare R2** recommandé (fallback local) |
| Monitoring | **Sentry** (erreurs + performances) |
| Email | SMTP configurable (Brevo recommandé) |
| Paiements | **FedaPay** (MTN MoMo, Moov, cartes — West Africa) + Stripe (optionnel) |
| Déploiement prod | cPanel + Passenger WSGI |
| Déploiement Docker | Dockerfile inclus |

---

## Démarrage en développement local

### 1. Prérequis

- Python 3.10 ou supérieur
- pip

### 2. Cloner et préparer l'environnement

```bash
cd qr-sales-management-tpe

# Créer l'environnement virtuel
python -m venv venv

# Activer (Linux/Mac)
source venv/bin/activate

# Activer (Windows)
venv\Scripts\activate

# Installer les dépendances
pip install -r requirements.txt
```

### 3. Configurer les variables d'environnement

```bash
cp .env.example .env
```

Ouvrir `.env` et renseigner au minimum :

```env
JWT_SECRET=un-secret-aleatoire-de-32-caracteres-minimum
SUPERADMIN_EMAIL=admin@votre-domaine.com
SUPERADMIN_PASSWORD=MotDePasseSecurise123!
```

### 4. Lancer le serveur

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

### Résolution — erreur "Unable to create process" (venv cassé)

Si le venv a été créé dans un ancien dossier ou que le projet a été déplacé, les chemins absolus à l'intérieur du venv sont invalides. La seule solution est de le recréer :

```bash
# 1. Supprimer l'ancien venv
rmdir /s /q venv

# 2. Recréer au bon endroit
python -m venv venv

# 3. Activer
venv\Scripts\activate

# 4. Réinstaller les dépendances
pip install -r requirements.txt

# 5. Lancer
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

> **Note** : les venvs Python contiennent des chemins absolus figés. Si tu déplaces le projet, tu dois toujours recréer le venv.

| URL | Description |
|-----|-------------|
| `http://localhost:8000` | Application |
| `http://localhost:8000/docs` | Documentation Swagger interactive |
| `http://localhost:8000/redoc` | Documentation ReDoc |
| `http://localhost:8000/health` | Statut de santé de l'API |

### 5. Pointer l'app Flutter sur le serveur local

Dans `QuickSellPay/lib/core/config/erp_config.dart` :

```dart
const String kErpBaseUrl = 'http://10.X.X.X:8000'; // votre IP locale
```

Trouver son IP locale : `ipconfig` (Windows) ou `ifconfig` (Mac/Linux).

---

## Déploiement Docker

```bash
# Construire l'image
docker build -t quicksellpay-api .

# Lancer le conteneur
docker run -d \
  --name quicksellpay-api \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/static:/app/static \
  quicksellpay-api
```

---

## Structure des routes

### Auth — `/auth/`

| Méthode | Route | Description |
|---------|-------|-------------|
| `POST` | `/auth/register` | Créer une boutique + compte admin |
| `POST` | `/auth/login` | Connexion (retourne JWT) |
| `POST` | `/auth/refresh` | Renouveler le token |
| `POST` | `/auth/forgot-password` | Demande de réinitialisation |
| `POST` | `/auth/reset-password` | Réinitialiser le mot de passe |
| `POST` | `/auth/invite` | Inviter un utilisateur |
| `POST` | `/auth/accept-invite` | Accepter une invitation |
| `GET` | `/auth/me` | Profil de l'utilisateur connecté |
| `GET` | `/auth/company-profile` | Branding de la boutique |
| `PATCH` | `/auth/company-profile` | Mettre à jour le branding |
| `POST` | `/auth/company-logo` | Uploader le logo |

### Produits — `/api/products/`

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/api/products` | Lister tous les produits |
| `POST` | `/api/products` | Créer un produit |
| `GET` | `/api/products/{id}` | Détail d'un produit |
| `PUT` | `/api/products/{id}` | Modifier un produit |
| `DELETE` | `/api/products/{id}` | Supprimer un produit |
| `PATCH` | `/api/products/{id}/stock` | Ajuster le stock |
| `POST` | `/api/products/{id}/image` | Uploader une image |
| `POST` | `/api/products/{id}/codes/generate` | Générer des QR codes |

### Ventes — `/api/sales/`

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/api/sales` | Historique des ventes |
| `POST` | `/api/sales` | Enregistrer une vente |
| `GET` | `/api/sales/{id}` | Détail d'une vente |
| `POST` | `/api/webhook/sale` | Webhook vente (sync hors-ligne) |

### Vérification QR — `/api/verify/`

| Méthode | Route | Description |
|---------|-------|-------------|
| `POST` | `/api/verify` | Vérifier l'authenticité d'un produit |
| `POST` | `/api/public/verify` | Vérification publique (sans auth) |
| `GET` | `/api/public/preview/{code}` | Aperçu public d'un produit |

### Billing — `/billing/`

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/billing/status` | Statut de l'abonnement |
| `POST` | `/billing/fedapay/checkout` | Creer un paiement FedaPay (MTN MoMo, Moov, carte) |
| `POST` | `/billing/fedapay/webhook` | Webhook FedaPay (activation automatique) |
| `POST` | `/billing/create-checkout-session` | Creer une session Stripe (optionnel) |
| `POST` | `/billing/webhook` | Webhook Stripe (optionnel) |

### Super-admin — `/admin/`

| Méthode | Route | Description |
|---------|-------|-------------|
| `GET` | `/admin/companies` | Lister toutes les boutiques |
| `GET` | `/admin/stats` | Statistiques globales |
| `PATCH` | `/admin/companies/{id}/plan` | Changer le plan d'une boutique |
| `PATCH` | `/admin/companies/{id}/suspend` | Suspendre une boutique |

---

## Plans et quotas

| Plan | Produits | Utilisateurs | Transactions/mois |
|------|----------|--------------|-------------------|
| FREE | 10 | 1 | 100 |
| BASIC | 100 | 3 | 1 000 |
| PRO | 1 000 | 10 | 10 000 |
| ENTERPRISE | Illimité | Illimité | Illimité |

---

## Variables d'environnement importantes

| Variable | Description | Défaut |
|----------|-------------|--------|
| `JWT_SECRET` | Clé secrète JWT (**obligatoire**) | — |
| `DB_ENGINE` | `sqlite` ou `mysql` | `sqlite` |
| `MYSQL_HOST` | Hôte MySQL (si `DB_ENGINE=mysql`) | `127.0.0.1` |
| `STORAGE_PROVIDER` | `local` ou `s3` | `local` |
| `MAIL_ENABLED` | Activer l'envoi d'emails | `false` |
| `STRIPE_SECRET_KEY` | Clé Stripe (abonnements) | — |
| `ALLOWED_ORIGINS` | Origines CORS autorisées | `localhost` |

Voir `.env.example` pour la liste complète.

---

## Déploiement sur cPanel (Tunelaf)

Le fichier `passenger_wsgi.py` est configuré pour le déploiement via **Passenger WSGI** sur cPanel. Le serveur est démarré automatiquement par cPanel — aucune commande manuelle nécessaire.

Pour forcer un redemarrage :

```bash
touch tmp/restart.txt
```

Lancer avec Gunicorn (hors cPanel Passenger) :

```bash
gunicorn -c gunicorn.conf.py main:app
```

Backup quotidien (ajouter dans cPanel -> Cron Jobs) :

```
0 2 * * * /home/tunelaf/quicksellpay/venv/bin/python /home/tunelaf/quicksellpay/backup.py >> /home/tunelaf/quicksellpay/logs/backup.log 2>&1
```

---

## Base de données

- **MySQL (production)** : base unique multi-tenant avec `company_id` sur toutes les tables. Configurer `DB_ENGINE=mysql` et les variables `MYSQL_*` dans `.env`.
- **SQLite (dev local)** : `DB_ENGINE=sqlite`, une base par boutique dans `./data/`. Aucune installation requise.

### Migration SQLite → MySQL

```bash
# 1. Configurer .env : DB_ENGINE=mysql, MYSQL_HOST, MYSQL_DATABASE, etc.
# 2. Lancer la migration (idempotente, utiliser INSERT IGNORE)
python migrate_to_mysql.py

# Migrer une seule boutique
python migrate_to_mysql.py --company demo-shop

# Simuler sans écrire
python migrate_to_mysql.py --dry-run
```

---

## Compte demo

```
Matricule : demo-shop
Mot de passe : AdminDemo123!
```


pip install -r requirements.txt
python migrate_to_mysql.py          # si tu avais des données SQLite
# Mettre DB_ENGINE=mysql dans .env
uvicorn main:app --reload