# Whether MorphoCloud's setup of an instance finished
# (SETUP_FINISHED_SIGNAL_DESIGN.md). Use with
#   jq -L <this directory> 'include "mc-setup"; … | mc_setup_done'
#
# Input: a server's properties as `openstack server show -f json`
# (.properties) or `openstack server list --long -f json` (.Properties)
# print them: an object, the flat string some client versions print instead
# (exoSetup='…', mcSetup='pending'), or null.
#
# - mcSetup present: finished only when it is "complete". Only the workflow
#   writes it: "pending" at server create (create-instance), "complete" in the
#   last setup step (setup-instance, "Set instance properties").
# - mcSetup absent: the old rule, exoSetup's status is "complete". Instances
#   created before mcSetup, and course repos on older branches, read as they
#   always did.
#
# exoSetup alone cannot mean "finished": the workflow writes it complete before
# the MyDrive mount, and the Exosphere web client writes it from the console log.

def mc_exo_setup_complete:
  if type == "object" then .status == "complete"
  elif type == "string" then (try (fromjson | .status == "complete") catch false)
  else false
  end;

def mc_setup_done:
  if type == "object" then
    if has("mcSetup") then (.mcSetup | tostring) == "complete"
    else (.exoSetup // null) | mc_exo_setup_complete
    end
  elif type == "string" then
    if test("\\bmcSetup=") then test("\\bmcSetup='complete'")
    else test("\\bexoSetup='[^']*\"status\": ?\"complete\"")
    end
  else false
  end;

# The exoSetup value as text, for its "error" check; with the flat string,
# the whole string (as the readers always matched it).
def mc_exo_setup_text:
  if type == "object" then (.exoSetup // "") | (if type == "string" then . else tojson end)
  elif type == "string" then .
  else ""
  end;
