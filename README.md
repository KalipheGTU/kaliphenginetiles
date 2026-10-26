# KalipheTiles (Windows 11)

Géoréférencement d'images (JPG/PNG/TIFF), collage de plusieurs images, tuiles XYZ / MBTiles / GeoPackage (256 ou 512 px),
GeoTIFF, Shapefile, GeoJSON (cadre), KMZ, world files, envoi S3.
Résultats : Documents\KalipheTiles\<id>\ (exports\ contient le cadre GeoJSON créé automatiquement).
Journal : Documents\KalipheTiles\kaliphetiles.log — bouton « 🔧 Diagnostic » dans l'application.
Raccourcis : Ctrl+Z / Ctrl+Y, Suppr, Échap, Ctrl+S.
# KalipheTiles

Application locale de géoréférencement d'images (JPG/PNG/TIFF), collage de plusieurs images, tuiles XYZ / MBTiles / GeoPackage,
GeoTIFF, Shapefile, GeoJSON, KMZ, world files et envoi S3.

## Ajouts de cette version
- Saisie de quatre points de contrôle (GCP) en pixels image et coordonnées géographiques EPSG:4326, avec contrôle des limites.
- Ajustement de l'affichage de l'image source : ajustement à la fenêtre après chargement, protection contre une taille de fenêtre nulle et message en cas d'erreur de chargement.
- Correction automatique optionnelle du contraste tonal de l'image, en préservant l'alpha.
- Bouton de relief 3D sur la carte, utilisant des tuiles d'altitude Terrarium publiques (connexion internet nécessaire).
- Panneau OpenTopography pour télécharger un MNT GeoTIFF sur une emprise WGS84 et avec une clé API personnelle.
- Bascule d'apparence sombre/clair.

## OpenTopography
Dans l'onglet « MNT / OpenTopography », renseignez une clé API personnelle, choisissez un modèle numérique de terrain, puis saisissez l'emprise Ouest/Sud/Est/Nord en longitude/latitude WGS84. Le bouton « Utiliser l'emprise de la carte » remplit ces limites à partir de l'étendue visible. Le résultat est téléchargé sous `opentopography_dem.tif`. La clé n'est pas enregistrée par l'application. La disponibilité des modèles et les limites de taille sont soumises aux conditions de l'API OpenTopography.

## Points de contrôle
Les valeurs géographiques initiales affichées (autour de 2°E, 48°N) sont des exemples : remplacez-les par vos coordonnées réelles. Cliquez sur « Remplir les coins de l'image » pour remplir les coordonnées pixel à partir des dimensions de l'image. Trois points non alignés suffisent mathématiquement pour une affine ; le quatrième aide à contrôler la cohérence.

## Données et exports
Résultats : `Documents\KalipheTiles\<id>\` (`exports\` contient les exports). L'export GeoTIFF utilise l'emprise du ou des calques sélectionnés et visibles conformément au flux d'export existant.
Journal : `Documents\KalipheTiles\kaliphetiles.log` — bouton « Diagnostic ».
Raccourcis : Ctrl+Z / Ctrl+Y, Suppr, Échap, Ctrl+S.
