### Directory [deploy](deploy) contains a standard deployment for Openshift 4.x (3.x is also working)

- Changes from the original
  - standard namespace is ocp-ops-view instead of default
  - create the namespace with an infra node selector, this is technically not necessary but nice to have
  - RunAsUser and RunAsNonRoot removed from kube-ops-view and redis deployment, Openshift deployments run with a random uid, choosing a specific one is unnecessary
  - added an emptydir to redis to create a directory which is writeable for the random user
  - added an edge encrypt route to expose the service via TLS only + redirect from port 80
  - set requests and limits a bit higher, your mileage may vary

### Directory [deploy-with-oauth-proxy](deploy-with-oauth-oauth-proxy) contains an additional oauth proxy to protect the application from unauthorized access

- Change from Openshift deploy
  - switched to reencrypt route to be able to use the service CA
  - added Openshift oauth proxy as a sidecar
  - added necessary service annotation for the Openshift oauth proxy
  - added service CA certificate to the proxy service to encrypt traffic between router and proxy
  - all users with permission to read the namespace are allowed to use the service by the oauth proxy
