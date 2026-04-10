# Zone Control Plugin — backend
# All zone-related code is isolated in this package.
# Integration hooks are limited to:
#   - start_server.py  : register zone_routes.router + call init_zone_store()
#   - streaming_service.py : apply active mask, classify zone hits
#   - recording_manager.py : accumulate + store zone metadata at clip close
