# Déploiement QuickSellPay sur cPanel

Ce workflow remplace l'ancienne installation SQLite de cPanel par la version QuickSellPay utilisant MySQL, Cloudflare R2, Brevo, Sentry et FedaPay.

Ne publiez jamais le fichier .env, les mots de passe ou les clés dans Git, WhatsApp ou e-mail.

## A. Sauvegarde avant remplacement

1. Dans cPanel > File Manager, compressez le dossier de l'ancienne application : quicksellpay-ancienne-version.zip.
2. Téléchargez ce ZIP sur votre ordinateur.
3. Exportez les données MySQL locales à transférer :

~~~
mysqldump -h 127.0.0.1 -P 3306 -u quicksellpay -p quicksellpay > quicksellpay-production.sql
~~~

## B. Base MySQL de production

Dans cPanel > MySQL Databases :

1. Créez une base, par exemple utilisateur_quicksellpay.
2. Créez un utilisateur MySQL avec un mot de passe fort.
3. Ajoutez l'utilisateur à la base avec ALL PRIVILEGES.
4. Dans phpMyAdmin, sélectionnez cette base puis Import et choisissez quicksellpay-production.sql.

Si vous ne transférez pas de données, l'application crée ses tables MySQL au premier démarrage.

## C. Envoyer le code

1. Créez un ZIP du dossier qr-sales-management-tpe.
2. Excluez venv, .env, data, backups, logs et les fichiers .db.
3. Dans File Manager, ouvrez le dossier Application root, par exemple :

~~~
/home/VOTRE_COMPTE/quicksellpay
~~~

4. Téléversez et extrayez le ZIP dans ce dossier.
5. Vérifiez la présence de main.py, passenger_wsgi.py, requirements.txt et frontend.

## D. Fichier .env de production

Créez .env dans Application root. Remplacez seulement les valeurs après le signe égal.

~~~
# App
JWT_SECRET=UNE_LONGUE_CLE_ALEATOIRE
SUPERADMIN_EMAIL=admin@votre-domaine.com
SUPERADMIN_PASSWORD=UN_MOT_DE_PASSE_FORT
ALLOWED_ORIGINS=https://quicksellpay.tunelaf.com
PUBLIC_APP_BASE_URL=https://quicksellpay.tunelaf.com

# MySQL
DB_ENGINE=mysql
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=utilisateur_quicksellpay
MYSQL_USER=utilisateur_quicksellpay
MYSQL_PASSWORD=VOTRE_MOT_DE_PASSE_MYSQL

# Cloudflare R2
STORAGE_PROVIDER=s3
STORAGE_BUCKET=quicksellpay-images
STORAGE_REGION=auto
STORAGE_ENDPOINT_URL=https://VOTRE_ACCOUNT_ID.r2.cloudflarestorage.com
STORAGE_ACCESS_KEY_ID=VOTRE_R2_ACCESS_KEY_ID
STORAGE_SECRET_ACCESS_KEY=VOTRE_R2_SECRET_ACCESS_KEY
STORAGE_PUBLIC_BASE_URL=https://VOTRE_URL_PUBLIQUE_R2.r2.dev
MAX_UPLOAD_SIZE_MB=10

# Brevo SMTP
MAIL_ENABLED=true
MAIL_HOST=smtp-relay.brevo.com
MAIL_PORT=587
MAIL_USERNAME=VOTRE_IDENTIFIANT_SMTP_BREVO
MAIL_PASSWORD=VOTRE_CLE_SMTP_BREVO
MAIL_FROM=UNE_ADRESSE_VERIFIEE@VOTRE_DOMAINE.COM
MAIL_USE_TLS=true

# Sentry
SENTRY_DSN=VOTRE_DSN_SENTRY
SENTRY_TRACES_SAMPLE_RATE=0.05

# Redis : laissez vide tant que le service n'est pas testé
REDIS_URL=

# FedaPay production
FEDAPAY_ENV=live
FEDAPAY_SECRET_KEY=VOTRE_CLE_SECRETE_LIVE_FEDAPAY
FEDAPAY_WEBHOOK_SECRET=VOTRE_SECRET_WEBHOOK_LIVE_FEDAPAY
FEDAPAY_PRICE_BASIC=5000
FEDAPAY_PRICE_PRO=15000
FEDAPAY_PRICE_ENTERPRISE=30000
BACKUP_DIR=./backups
~~~

Pour des tests seulement, utilisez FEDAPAY_ENV=sandbox et les clés Sandbox. En production, utilisez live.

## E. Application Python cPanel

Dans Setup Python App :

| Champ | Valeur |
|---|---|
| Python | 3.11 ou version compatible |
| Application root | /home/VOTRE_COMPTE/quicksellpay |
| Application URL | https://quicksellpay.tunelaf.com |
| Startup file | passenger_wsgi.py |
| Entry point | application |

Dans le terminal cPanel, activez le virtualenv fourni par cPanel puis lancez :

~~~
cd /home/VOTRE_COMPTE/quicksellpay
python cpanel_setup.py
python cpanel_diagnose.py
~~~

Le diagnostic doit finir par Diagnostic termine avec succes. Cliquez ensuite sur Restart dans Setup Python App.

## F. Configurer les services

### Cloudflare R2

- Bucket : quicksellpay-images.
- Jeton R2 : Object Read and Write sur ce bucket.
- Ouvrez une image publique dans un navigateur pour confirmer l'URL publique.

### Brevo

1. Paramètres > Senders, Domains, IPs.
2. Vérifiez MAIL_FROM ou authentifiez votre domaine SPF/DKIM.
3. Après une invitation, consultez Transactional > Email > Logs.

### Sentry

Collez le DSN du projet FastAPI dans SENTRY_DSN.

### FedaPay

Dans le compte FedaPay Live, configurez le webhook :

~~~
https://quicksellpay.tunelaf.com/billing/fedapay/webhook
~~~

Un plan FedaPay est actif 31 jours puis revient automatiquement à Free à son échéance.

## G. Tests avant ouverture publique

1. Ouvrez https://quicksellpay.tunelaf.com/health.
2. Connectez-vous et créez un produit.
3. Changez son image : elle doit apparaître dans la boutique et la page de provenance.
4. Créez une invitation et contrôlez son statut dans Brevo Transactional Logs.
5. Téléchargez les PDF produits, ventes et stock.
6. Effectuez un paiement FedaPay dans le même environnement que FEDAPAY_ENV.
7. Vérifiez plan, quotas et date d'expiration.
8. Vérifiez dans FedaPay que le webhook répond HTTP 200.

## H. Sauvegarde quotidienne

Dans cPanel > Cron Jobs, adaptez ce chemin à votre compte :

~~~
0 2 * * * /home/VOTRE_COMPTE/virtualenv/quicksellpay/3.11/bin/python /home/VOTRE_COMPTE/quicksellpay/backup.py >> /home/VOTRE_COMPTE/quicksellpay/logs/backup.log 2>&1
~~~

Ce script sauvegarde MySQL. Les images restent aussi dans Cloudflare R2 : ne supprimez pas le bucket.

## I. Retour arrière

1. Stoppez l'application Python dans cPanel.
2. Restaurez quicksellpay-ancienne-version.zip.
3. Restaurez son .env.
4. Redémarrez l'application.

Ne remplacez pas MySQL par SQLite en production.
