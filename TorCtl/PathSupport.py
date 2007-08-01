#!/usr/bin/python

import TorCtl
import re
import struct
import random
import socket
import copy
import Queue
import time
from TorUtil import *

__all__ = ["NodeRestrictionList", "PathRestrictionList",
"PercentileRestriction", "OSRestriction", "ConserveExitsRestriction",
"FlagsRestriction", "MinBWRestriction", "VersionIncludeRestriction",
"VersionExcludeRestriction", "ExitPolicyRestriction", "OrNodeRestriction",
"AtLeastNNodeRestriction", "NotNodeRestriction", "Subnet16Restriction",
"UniqueRestriction", "UniformGenerator", "OrderedExitGenerator",
"BwWeightedGenerator", "PathSelector", "Connection", "NickRestriction", 
"IdHexRestriction", "PathBuilder", "CircuitHandler", "StreamHandler", 
"SelectionManager", "CountryCodeRestriction", "CountryRestriction", 
"UniqueCountryRestriction", "SingleCountryRestriction", 
"ContinentRestriction", "ContinentJumperRestriction", 
"UniqueContinentRestriction"]

#################### Path Support Interfaces #####################

class NodeRestriction:
  "Interface for node restriction policies"
  def r_is_ok(self, r): return True  

class NodeRestrictionList:
  def __init__(self, restrictions):
    self.restrictions = restrictions

  def r_is_ok(self, r):
    for rs in self.restrictions:
      if not rs.r_is_ok(r): return False
    return True

  def add_restriction(self, restr):
    self.restrictions.append(restr)

  # TODO: This does not collapse meta restrictions..
  def del_restriction(self, RestrictionClass):
    self.restrictions = filter(
        lambda r: not isinstance(r, RestrictionClass),
          self.restrictions)

class PathRestriction:
  "Interface for path restriction policies"
  def path_is_ok(self, path): return True  

class PathRestrictionList:
  def __init__(self, restrictions):
    self.restrictions = restrictions
  
  def path_is_ok(self, path):
    for rs in self.restrictions:
      if not rs.path_is_ok(path):
        return False
    return True

  def add_restriction(self, rstr):
    self.restrictions.append(rstr)

  def del_restriction(self, RestrictionClass):
    self.restrictions = filter(
        lambda r: not isinstance(r, RestrictionClass),
          self.restrictions)

class NodeGenerator:
  "Interface for node generation"
  def __init__(self, sorted_r, rstr_list):
    self.rstr_list = rstr_list # Check me before you yield!
    self.sorted_r = sorted_r
    self.rewind()

  def reset_restriction(self, rstr_list):
    self.rstr_list = rstr_list

  def rewind(self):
    self.routers = copy.copy(self.sorted_r)

  def mark_chosen(self, r):
    self.routers.remove(r)

  def all_chosen(self):
    return not self.routers

  def next_r(self): raise NotImplemented()

class Connection(TorCtl.Connection):
  def build_circuit(self, pathlen, path_sel):
    circ = Circuit()
    circ.path = path_sel.build_path(pathlen)
    circ.exit = circ.path[pathlen-1]
    circ.circ_id = self.extend_circuit(0, circ.id_path())
    return circ

######################## Node Restrictions ########################

# TODO: We still need more path support implementations
#  - NodeRestrictions:
#    - Uptime/LongLivedPorts (Does/should hibernation count?)
#    - Published/Updated
#  - PathRestrictions:
#    - Family
#    - GeoIP:
#      - OceanPhobicRestrictor (avoids Pacific Ocean or two atlantic crossings)
#        or ContinentRestrictor (avoids doing more than N continent crossings)
#        - Mathematical/empirical study of predecessor expectation
#          - If middle node on the same continent as exit, exit learns nothing
#          - else, exit has a bias on the continent of origin of user
#            - Language and browser accept string determine this anyway
#      - EchelonPhobicRestrictor
#        - Does not cross international boundaries for client->Entry or
#          Exit->destination hops

class PercentileRestriction(NodeRestriction):
  def __init__(self, pct_skip, pct_fast, r_list):
    self.pct_fast = pct_fast
    self.pct_skip = pct_skip
    self.sorted_r = r_list

  def r_is_ok(self, r):
    # Hrmm.. technically we shouldn't count non-running routers in this..
    # but that is tricky to do efficiently
    
    if r.list_rank < len(self.sorted_r)*self.pct_skip/100: return False
    elif r.list_rank > len(self.sorted_r)*self.pct_fast/100: return False
    
    return True
    
class OSRestriction(NodeRestriction):
  def __init__(self, ok, bad=[]):
    self.ok = ok
    self.bad = bad

  def r_is_ok(self, r):
    for y in self.ok:
      if re.search(y, r.os):
        return True
    for b in self.bad:
      if re.search(b, r.os):
        return False
    if self.ok: return False
    if self.bad: return True

class ConserveExitsRestriction(NodeRestriction):
  # FIXME: Make this adaptive
  def r_is_ok(self, r): return not "Exit" in r.flags

class FlagsRestriction(NodeRestriction):
  def __init__(self, mandatory, forbidden=[]):
    self.mandatory = mandatory
    self.forbidden = forbidden

  def r_is_ok(self, router):
    for m in self.mandatory:
      if not m in router.flags: return False
    for f in self.forbidden:
      if f in router.flags: return False
    return True

class NickRestriction(NodeRestriction):
  """Require that the node nickname is as specified"""
  def __init__(self, nickname):
    self.nickname = nickname

  def r_is_ok(self, router):
    return router.nickname == self.nickname

class IdHexRestriction(NodeRestriction):
  """Require that the node idhash is as specified"""
  def __init__(self, idhex):
    if idhex[0] == '$':
      self.idhex = idhex[1:].upper()
    else:
      self.idhex = idhex.upper()

  def r_is_ok(self, router):
    return router.idhex == self.idhex
  
class MinBWRestriction(NodeRestriction):
  def __init__(self, minbw):
    self.min_bw = minbw

  def r_is_ok(self, router): return router.bw >= self.min_bw
   
class VersionIncludeRestriction(NodeRestriction):
  def __init__(self, eq):
    self.eq = map(TorCtl.RouterVersion, eq)
  
  def r_is_ok(self, router):
    for e in self.eq:
      if e == router.version:
        return True
    return False

class VersionExcludeRestriction(NodeRestriction):
  def __init__(self, exclude):
    self.exclude = map(TorCtl.RouterVersion, exclude)
  
  def r_is_ok(self, router):
    for e in self.exclude:
      if e == router.version:
        return False
    return True

class VersionRangeRestriction(NodeRestriction):
  def __init__(self, gr_eq, less_eq=None):
    self.gr_eq = TorCtl.RouterVersion(gr_eq)
    if less_eq: self.less_eq = TorCtl.RouterVersion(less_eq)
    else: self.less_eq = None
  
  def r_is_ok(self, router):
    return (not self.gr_eq or router.version >= self.gr_eq) and \
        (not self.less_eq or router.version <= self.less_eq)

class ExitPolicyRestriction(NodeRestriction):
  def __init__(self, to_ip, to_port):
    self.to_ip = to_ip
    self.to_port = to_port

  def r_is_ok(self, r): return r.will_exit_to(self.to_ip, self.to_port)

class MetaNodeRestriction(NodeRestriction):
  # TODO: these should collapse the restriction and return a new
  # instance for re-insertion (or None)
  def next_rstr(self): raise NotImplemented()
  def del_restriction(self, RestrictionClass): raise NotImplemented()

class OrNodeRestriction(MetaNodeRestriction):
  def __init__(self, rs):
    self.rstrs = rs

  def r_is_ok(self, r):
    for rs in self.rstrs:
      if rs.r_is_ok(r):
        return True
    return False

class NotNodeRestriction(MetaNodeRestriction):
  def __init__(self, a):
    self.a = a

  def r_is_ok(self, r): return not self.a.r_is_ok(r)

class AtLeastNNodeRestriction(MetaNodeRestriction):
  def __init__(self, rstrs, n):
    self.rstrs = rstrs
    self.n = n

  def r_is_ok(self, r):
    cnt = 0
    for rs in self.rstrs:
      if rs.r_is_ok(r):
        cnt += 1
    if cnt < self.n: return False
    else: return True


#################### Path Restrictions #####################

class Subnet16Restriction(PathRestriction):
  def path_is_ok(self, path):
    mask16 = struct.unpack(">I", socket.inet_aton("255.255.0.0"))[0]
    ip16 = path[0].ip & mask16
    for r in path[1:]:
      if ip16 == (r.ip & mask16):
        return False
    return True

class UniqueRestriction(PathRestriction):
  def path_is_ok(self, path):
    for i in xrange(0,len(path)):
      if path[i] in path[:i]:
        return False
    return True

#################### GeoIP Restrictions ###################

class CountryCodeRestriction(NodeRestriction):
  """ Ensure that the country_code is set """
  def r_is_ok(self, r):
    return r.country_code != None

class CountryRestriction(NodeRestriction):
  """ Ensure a specific country_code for nodes """
  def __init__(self, country_code):
    self.country_code = country_code

  def r_is_ok(self, r):
    return r.country_code == self.country_code

class ExcludeCountriesRestriction(NodeRestriction):
  """ Exclude a list of countries """
  def __init__(self, countries):
    self.countries = countries

  def r_is_ok(self, r):
    return not (r.country_code in self.countries)

class UniqueCountryRestriction(PathRestriction):
  """ Ensure every router to have a distinct country_code """
  def path_is_ok(self, path):
    for i in xrange(0, len(path)-1):
      for j in xrange(i+1, len(path)):
        if path[i].country_code == path[j].country_code:
          return False;
    return True;

class SingleCountryRestriction(PathRestriction):
  """ Ensure every router to have the same country_code """
  def path_is_ok(self, path):
    country_code = path[0].country_code
    for r in path:
      if country_code != r.country_code:
        return False
    return True

class ContinentRestriction(PathRestriction):
  """ Do not more than n continent crossings """
  def __init__(self, n):
    self.n = n

  def path_is_ok(self, path):
    crossings = 0
    prev = None
    # Compute crossings until now
    for r in path:
      # Jump over the first router
      if prev:
        if r.continent != prev.continent:
          crossings += 1
      prev = r
    if crossings > self.n: return False
    else: return True

class ContinentJumperRestriction(PathRestriction):
  """ Ensure continent crossings between all hops """
  def path_is_ok(self, path):
    prev = None
    # Compute crossings until now
    for r in path:
      # Jump over the first router
      if prev:
        if r.continent == prev.continent:
          return False
      prev = r
    return True

class UniqueContinentRestriction(PathRestriction):
  """ Ensure every hop to be on a different continent """
  def path_is_ok(self, path):
    for i in xrange(0, len(path)-1):
      for j in xrange(i+1, len(path)):
        if path[i].continent == path[j].continent:
          return False;
    return True;

#################### Node Generators ######################

class UniformGenerator(NodeGenerator):
  def next_r(self):
    while not self.all_chosen():
      r = random.choice(self.routers)
      if self.rstr_list.r_is_ok(r): yield r

class OrderedExitGenerator(NodeGenerator):
  def __init__(self, to_port, sorted_r, rstr_list):
    self.to_port = to_port
    self.next_exit_by_port = {}
    NodeGenerator.__init__(self, sorted_r, rstr_list)

  def rewind(self):
    if self.to_port not in self.next_exit_by_port or not self.next_exit_by_port[self.to_port]:
      self.next_exit_by_port[self.to_port] = 0
      self.last_idx = len(self.sorted_r)
    else:
      self.last_idx = self.next_exit_by_port[self.to_port]

  def set_port(self, port):
    self.to_port = port
    self.rewind()
     
  def mark_chosen(self, r):
    self.next_exit_by_port[self.to_port] += 1
  
  def all_chosen(self):
    return self.last_idx == self.next_exit_by_port[self.to_port]

  def next_r(self):
    while True: # A do..while would be real nice here..
      if self.next_exit_by_port[self.to_port] >= len(self.sorted_r):
        self.next_exit_by_port[self.to_port] = 0
      r = self.sorted_r[self.next_exit_by_port[self.to_port]]
      if self.rstr_list.r_is_ok(r): yield r
      else: self.next_exit_by_port[self.to_port] += 1
      if self.last_idx == self.next_exit_by_port[self.to_port]:
        break

class BwWeightedGenerator(NodeGenerator):
  """ Pass exit=True to create a generator for exit-nodes """
  def __init__(self, sorted_r, rstr_list, pathlen, exit=False):
    # Out for an exit-node?
    self.exit = exit
    # Different sums of bandwidths
    self.total_bw = 0
    self.total_exit_bw = 0
    self.exit_bw_to_dest = 0
    self.pathlen = pathlen
    NodeGenerator.__init__(self, sorted_r, rstr_list)

  def rewind(self):
    NodeGenerator.rewind(self)
    # Set the exit_weight
    if self.exit:
      self.exit_bw_to_dest = 0
      # We are choosing an exit-node
      for r in self.sorted_r:
        if self.rstr_list.r_is_ok(r):
          self.exit_bw_to_dest += r.bw
      plog("DEBUG", "Exit-bandwidth to this destination is " +
         str(self.exit_bw_to_dest))
      self.weight = 1.0
    else:
      # We are choosing a non-exit
      self.total_exit_bw = 0
      self.total_bw = 0
      for r in self.sorted_r:
        # Should this be outside the restriction checks?
        if self.rstr_list.r_is_ok(r):
          self.total_bw += r.bw
          if "Exit" in r.flags:
            self.total_exit_bw += r.bw

      bw_per_hop = (1.0*self.total_bw)/self.pathlen
      ratio = self.total_exit_bw/float(self.total_bw)
      plog("DEBUG", "E = " + str(self.total_exit_bw) +
         ", T = " + str(self.total_bw) +
         ", ratio = " + str(ratio) +
         ", bw_per_hop = " + str(bw_per_hop))
      
      if self.total_exit_bw < bw_per_hop:
        # Don't use exit nodes at all
        # add a ConserveExitsRestriction?
        self.weight = 0
      else:
        self.weight = ((self.total_exit_bw-bw_per_hop)/self.total_exit_bw)
    plog("DEBUG", "The exit-weight is: " + str(self.weight))

  def next_r(self):
    while True:
      # Choose a suitable random int
      if self.exit:
        i = random.randint(0, self.exit_bw_to_dest)
      else:
        i = random.randint(0,
                     (self.total_bw-self.total_exit_bw)    # nonexit
                     +int(self.total_exit_bw*self.weight)) # + weighted exit
      # Go through the routers
      for r in self.routers:
        # Below zero here --> choose a new random int+router
        if i < 0: break
        if self.rstr_list.r_is_ok(r):
          # Only weight exit nodes
          if "Exit" in r.flags:
            i -= self.weight * r.bw
          else: i -= r.bw
          if i < 0:
            plog("DEBUG", "Chosen router with a bandwidth of: " + str(r.bw))
            yield r

####################### Secret Sauce ###########################

class PathError(Exception):
  pass

class NoRouters(PathError):
  pass

class PathSelector:
  "Implementation of path selection policies"
  def __init__(self, entry_gen, mid_gen, exit_gen, path_restrict):
    self.entry_gen = entry_gen
    self.mid_gen = mid_gen
    self.exit_gen = exit_gen
    self.path_restrict = path_restrict

  def build_path(self, pathlen):
    self.entry_gen.rewind()
    self.mid_gen.rewind()
    self.exit_gen.rewind()
    entry = self.entry_gen.next_r()
    mid = self.mid_gen.next_r()
    ext = self.exit_gen.next_r()

    while True:
      path = []
      try:
        if pathlen == 1:
          path = [ext.next()]
        else:
          path.append(entry.next())
          for i in xrange(1, pathlen-1):
            path.append(mid.next())
          path.append(ext.next())
        if self.path_restrict.path_is_ok(path):
          self.entry_gen.mark_chosen(path[0])
          for i in xrange(1, pathlen-1):
            self.mid_gen.mark_chosen(path[i])
          self.exit_gen.mark_chosen(path[pathlen-1])
          break
      except StopIteration:
        plog("NOTICE", "Ran out of routers during buildpath..");
        self.entry_gen.rewind()
        self.mid_gen.rewind()
        self.exit_gen.rewind()
        entry = self.entry_gen.next_r()
        mid = self.entry_gen.next_r()
        ext = self.entry_gen.next_r()
    return path

class SelectionManager:
  """Helper class to handle configuration updates
    
    The methods are NOT threadsafe. They may ONLY be called from
    EventHandler's thread.

    To update the selection manager, schedule a config update job
    using PathBuilder.schedule_selmgr() with a worker function
    to modify this object.
    """
  def __init__(self, pathlen, order_exits,
         percent_fast, percent_skip, min_bw, use_all_exits,
         uniform, use_exit, use_guards, geoip_config=None):
    self.__ordered_exit_gen = None 
    self.pathlen = pathlen
    self.order_exits = order_exits
    self.percent_fast = percent_fast
    self.percent_skip = percent_skip
    self.min_bw = min_bw
    self.use_all_exits = use_all_exits
    self.uniform = uniform
    self.exit_name = use_exit
    self.use_guards = use_guards
    self.geoip_config = geoip_config

  def reconfigure(self, sorted_r):
    if self.use_all_exits:
      self.path_rstr = PathRestrictionList([UniqueRestriction()])
    else:
      self.path_rstr = PathRestrictionList(
           [Subnet16Restriction(), UniqueRestriction()])
  
    if self.use_guards: entry_flags = ["Guard", "Valid", "Running"]
    else: entry_flags = ["Valid", "Running"]
      
    entry_rstr = NodeRestrictionList(
      [PercentileRestriction(self.percent_skip, self.percent_fast, sorted_r),
       ConserveExitsRestriction(),
       FlagsRestriction(entry_flags, [])]
    )
    mid_rstr = NodeRestrictionList(
      [PercentileRestriction(self.percent_skip, self.percent_fast, sorted_r),
       ConserveExitsRestriction(),
       FlagsRestriction(["Running"], [])]
    )
    if self.use_all_exits:
      self.exit_rstr = NodeRestrictionList(
        [FlagsRestriction(["Valid", "Running"], ["BadExit"])])
    else:
      self.exit_rstr = NodeRestrictionList(
        [PercentileRestriction(self.percent_skip, self.percent_fast, sorted_r),
         FlagsRestriction(["Valid", "Running"], ["BadExit"])])

    if self.exit_name:
      self.exit_rstr.del_restriction(IdHexRestriction)
      self.exit_rstr.del_restriction(NickRestriction)
      if self.exit_name[0] == '$':
        self.exit_rstr.add_restriction(IdHexRestriction(self.exit_name))
      else:
        self.exit_rstr.add_restriction(NickRestriction(self.exit_name))

    # GeoIP configuration
    if self.geoip_config:
      # Every node needs country_code 
      entry_rstr.add_restriction(CountryCodeRestriction())
      mid_rstr.add_restriction(CountryCodeRestriction())
      self.exit_rstr.add_restriction(CountryCodeRestriction())
      
      # First hop in a specified country?
      if self.geoip_config.entry_country:  
        entry_rstr.add_restriction(CountryRestriction(self.geoip_config.entry_country))
      # Last hop in a specified country?
      if self.geoip_config.exit_country:
        self.exit_rstr.add_restriction(CountryRestriction(self.geoip_config.exit_country))

      # Excluded countries
      if self.geoip_config.excludes:
        plog("INFO", "Excluded countries: " + str(self.geoip_config.excludes))
        if len(self.geoip_config.excludes) > 0:
          entry_rstr.add_restriction(ExcludeCountriesRestriction(self.geoip_config.excludes))
          mid_rstr.add_restriction(ExcludeCountriesRestriction(self.geoip_config.excludes))
          self.exit_rstr.add_restriction(ExcludeCountriesRestriction(self.geoip_config.excludes))      
      
      # Unique countries set? None --> pass
      if self.geoip_config.unique_countries != None:
        if self.geoip_config.unique_countries:
          # If True: unique countries 
          self.path_rstr.add_restriction(UniqueCountryRestriction())
        else:
          # False: use the same country for all nodes in a path
          self.path_rstr.add_restriction(SingleCountryRestriction())
      
      # Specify max number of crossings here, None means UniqueContinents
      if self.geoip_config.max_crossings == None:
        self.path_rstr.add_restriction(UniqueContinentRestriction())
      else: self.path_rstr.add_restriction(ContinentRestriction(self.geoip_config.max_crossings))
    
    # This is kind of hokey..
    if self.order_exits:
      if self.__ordered_exit_gen:
        exitgen = self.__ordered_exit_gen
        exitgen.reset_restriction(self.exit_rstr)
      else:
        exitgen = self.__ordered_exit_gen = \
          OrderedExitGenerator(80, sorted_r, self.exit_rstr)
    elif self.uniform:
      exitgen = UniformGenerator(sorted_r, self.exit_rstr)
    else:
      exitgen = BwWeightedGenerator(sorted_r, self.exit_rstr, self.pathlen, exit=True)

    if self.uniform:
      self.path_selector = PathSelector(
         UniformGenerator(sorted_r, entry_rstr),
         UniformGenerator(sorted_r, mid_rstr),
         exitgen, self.path_rstr)
    else:
      # Remove ConserveExitsRestrictions for entry and middle positions
      entry_rstr.del_restriction(ConserveExitsRestriction)
      mid_rstr.del_restriction(ConserveExitsRestriction)
      # Initially setup the PathSelector to port 80 and return      
      self.exit_rstr.add_restriction(ExitPolicyRestriction("255.255.255.255", 80))
      self.path_selector = PathSelector(
         BwWeightedGenerator(sorted_r, entry_rstr, self.pathlen),
         BwWeightedGenerator(sorted_r, mid_rstr, self.pathlen),
         exitgen, self.path_rstr)
      return

  def set_target(self, ip, port):
    self.exit_rstr.del_restriction(ExitPolicyRestriction)
    self.exit_rstr.add_restriction(ExitPolicyRestriction(ip, port))
    if self.__ordered_exit_gen: self.__ordered_exit_gen.set_port(port)

class Circuit:
  def __init__(self):
    self.circ_id = 0
    self.path = [] # routers
    self.exit = None
    self.built = False
    self.dirty = False
    self.closed = False
    self.detached_cnt = 0
    self.last_extended_at = time.time()
    self.extend_times = []      # List of all extend-durations
    self.setup_duration = None  # Sum of extend-times
    self.pending_streams = []   # Which stream IDs are pending us
  
  def id_path(self): return map(lambda r: r.idhex, self.path)

class Stream:
  def __init__(self, sid, host, port, kind):
    self.strm_id = sid
    self.detached_from = [] # circ id #'s
    self.pending_circ = None
    self.circ = None
    self.host = host
    self.port = port
    self.kind = kind
    self.attached_at = 0
    self.bytes_read = 0
    self.bytes_written = 0
    self.failed = False
    self.failed_reason = None # Cheating a little.. Only used by StatsHandler

  def lifespan(self, now): return now-self.attached_at

# TODO: Make passive "PathWatcher" so people can get aggregate 
# node reliability stats for normal usage without us attaching streams

class PathBuilder(TorCtl.EventHandler):
  """
  PathBuilder implementation. Handles circuit construction, subject
  to the constraints of the SelectionManager selmgr.
  
  Do not access this object from other threads. Instead, use the 
  schedule_* functions to schedule work to be done in the thread
  of the EventHandler.
  """
  def __init__(self, c, selmgr, RouterClass):
    TorCtl.EventHandler.__init__(self)
    self.c = c
    nslist = c.get_network_status()
    self.last_exit = None
    self.new_nym = False
    self.resolve_port = 0
    self.num_circuits = 1
    self.RouterClass = RouterClass
    self.sorted_r = []
    self.name_to_key = {}
    self.routers = {}
    self.circuits = {}
    self.streams = {}
    self.read_routers(nslist)
    self.selmgr = selmgr
    self.selmgr.reconfigure(self.sorted_r)
    self.imm_jobs = Queue.Queue()
    self.low_prio_jobs = Queue.Queue()
    self.run_all_jobs = False
    self.do_reconfigure = False
    plog("INFO", "Read "+str(len(self.sorted_r))+"/"+str(len(nslist))+" routers")

  def schedule_immediate(self, job):
    """
    Schedules an immediate job to be run before the next event is
    processed.
    """
    self.imm_jobs.put(job)

  def schedule_low_prio(self, job):
    """
    Schedules a job to be run when a non-time critical event arrives.
    """
    self.low_prio_jobs.put(job)

  def schedule_selmgr(self, job):
    """
    Schedules an immediate job to be run before the next event is
    processed. Also notifies the selection manager that it needs
    to update itself.
    """
    def notlambda(this):
      job(this.selmgr)
      this.do_reconfigure = True
    self.schedule_immediate(notlambda)

     
  def heartbeat_event(self, event):
    while not self.imm_jobs.empty():
      imm_job = self.imm_jobs.get_nowait()
      imm_job(self)
    
    if self.do_reconfigure:
      self.selmgr.reconfigure(self.sorted_r)
      self.do_reconfigure = False
    
    if self.run_all_jobs:
      self.run_all_jobs = False
      while not self.low_prio_jobs.empty():
        imm_job = self.low_prio_jobs.get_nowait()
        imm_job(self)
      return
    
    # If event is stream:NEW*/DETACHED or circ BUILT/FAILED, 
    # don't run low prio jobs.. No need to delay streams for them.
    if isinstance(event, TorCtl.CircuitEvent):
      if event.status in ("BUILT", "FAILED"):
        return
    elif isinstance(event, TorCtl.StreamEvent):
      if event.status in ("NEW", "NEWRESOLVE", "DETACHED"):
        return
    
    # Do the low prio jobs one at a time in case a 
    # higher priority event is queued   
    if not self.low_prio_jobs.empty():
      delay_job = self.low_prio_jobs.get_nowait()
      delay_job(self)

  def read_routers(self, nslist):
    routers = self.c.read_routers(nslist)
    new_routers = []
    for r in routers:
      self.name_to_key[r.nickname] = "$"+r.idhex
      if r.idhex in self.routers:
        if self.routers[r.idhex].nickname != r.nickname:
          plog("NOTICE", "Router "+r.idhex+" changed names from "
             +self.routers[r.idhex].nickname+" to "+r.nickname)
        # Must do IN-PLACE update to keep all the refs to this router
        # valid and current (especially for stats)
        self.routers[r.idhex].update_to(r)
      else:
        rc = self.RouterClass(r)
        self.routers[rc.idhex] = rc
        new_routers.append(rc)
    self.sorted_r.extend(new_routers)
    self.sorted_r.sort(lambda x, y: cmp(y.bw, x.bw))
    for i in xrange(len(self.sorted_r)): self.sorted_r[i].list_rank = i

  def attach_stream_any(self, stream, badcircs):
    # Newnym, and warn if not built plus pending
    unattached_streams = [stream]
    if self.new_nym:
      self.new_nym = False
      plog("DEBUG", "Obeying new nym")
      for key in self.circuits.keys():
        if (not self.circuits[key].dirty
            and len(self.circuits[key].pending_streams)):
          plog("WARN", "New nym called, destroying circuit "+str(key)
             +" with "+str(len(self.circuits[key].pending_streams))
             +" pending streams")
          unattached_streams.extend(self.circuits[key].pending_streams)
          self.circuits[key].pending_streams.clear()
        # FIXME: Consider actually closing circ if no streams.
        self.circuits[key].dirty = True
      
    for circ in self.circuits.itervalues():
      if circ.built and not circ.dirty and circ.circ_id not in badcircs:
        if circ.exit.will_exit_to(stream.host, stream.port):
          try:
            self.c.attach_stream(stream.strm_id, circ.circ_id)
            stream.pending_circ = circ # Only one possible here
            circ.pending_streams.append(stream)
          except TorCtl.ErrorReply, e:
            # No need to retry here. We should get the failed
            # event for either the circ or stream next
            plog("WARN", "Error attaching stream: "+str(e.args))
            return
          break
    else:
      circ = None
      self.selmgr.set_target(stream.host, stream.port)
      while circ == None:
        try:
          circ = self.c.build_circuit(
                  self.selmgr.pathlen,
                  self.selmgr.path_selector)
        except TorCtl.ErrorReply, e:
          # FIXME: How come some routers are non-existant? Shouldn't
          # we have gotten an NS event to notify us they
          # disappeared?
          plog("NOTICE", "Error building circ: "+str(e.args))
      for u in unattached_streams:
        plog("DEBUG",
           "Attaching "+str(u.strm_id)+" pending build of "+str(circ.circ_id))
        u.pending_circ = circ
      circ.pending_streams.extend(unattached_streams)
      self.circuits[circ.circ_id] = circ
    self.last_exit = circ.exit

  def circ_status_event(self, c):
    output = [c.event_name, str(c.circ_id), c.status]
    if c.path: output.append(",".join(c.path))
    if c.reason: output.append("REASON=" + c.reason)
    if c.remote_reason: output.append("REMOTE_REASON=" + c.remote_reason)
    plog("DEBUG", " ".join(output))
    # Circuits we don't control get built by Tor
    if c.circ_id not in self.circuits:
      plog("DEBUG", "Ignoring circ " + str(c.circ_id))
      return
    if c.status == "EXTENDED":
      self.circuits[c.circ_id].last_extended_at = c.arrived_at
    elif c.status == "FAILED" or c.status == "CLOSED":
      # XXX: Can still get a STREAM FAILED for this circ after this
      circ = self.circuits[c.circ_id]
      del self.circuits[c.circ_id]
      for stream in circ.pending_streams:
        plog("DEBUG", "Finding new circ for " + str(stream.strm_id))
        self.attach_stream_any(stream, stream.detached_from)
    elif c.status == "BUILT":
      self.circuits[c.circ_id].built = True
      try:
        for stream in self.circuits[c.circ_id].pending_streams:
          self.c.attach_stream(stream.strm_id, c.circ_id)
      except TorCtl.ErrorReply, e:
        # No need to retry here. We should get the failed
        # event for either the circ or stream next
        plog("WARN", "Error attaching stream: "+str(e.args))
        return

  def stream_status_event(self, s):
    output = [s.event_name, str(s.strm_id), s.status, str(s.circ_id),
          s.target_host, str(s.target_port)]
    if s.reason: output.append("REASON=" + s.reason)
    if s.remote_reason: output.append("REMOTE_REASON=" + s.remote_reason)
    plog("DEBUG", " ".join(output))
    if not re.match(r"\d+.\d+.\d+.\d+", s.target_host):
      s.target_host = "255.255.255.255" # ignore DNS for exit policy check
    if s.status == "NEW" or s.status == "NEWRESOLVE":
      if s.status == "NEWRESOLVE" and not s.target_port:
        s.target_port = self.resolve_port
      self.streams[s.strm_id] = Stream(s.strm_id, s.target_host, s.target_port, s.status)

      self.attach_stream_any(self.streams[s.strm_id],
                   self.streams[s.strm_id].detached_from)
    elif s.status == "DETACHED":
      if s.strm_id not in self.streams:
        plog("WARN", "Detached stream "+str(s.strm_id)+" not found")
        self.streams[s.strm_id] = Stream(s.strm_id, s.target_host,
                      s.target_port, "NEW")
      # FIXME Stats (differentiate Resolved streams also..)
      if not s.circ_id:
        plog("WARN", "Stream "+str(s.strm_id)+" detached from no circuit!")
      else:
        self.streams[s.strm_id].detached_from.append(s.circ_id)
      
      if self.streams[s.strm_id] in self.streams[s.strm_id].pending_circ.pending_streams:
        self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      self.streams[s.strm_id].pending_circ = None
      self.attach_stream_any(self.streams[s.strm_id],
                   self.streams[s.strm_id].detached_from)
    elif s.status == "SUCCEEDED":
      if s.strm_id not in self.streams:
        plog("NOTICE", "Succeeded stream "+str(s.strm_id)+" not found")
        return
      if s.circ_id and self.streams[s.strm_id].pending_circ.circ_id != s.circ_id:
        # Hrmm.. this can happen on a new-nym.. Very rare, putting warn
        # in because I'm still not sure this is correct
        plog("WARN", "Mismatch of pending: "
          +str(self.streams[s.strm_id].pending_circ.circ_id)+" vs "
          +str(s.circ_id))
        self.streams[s.strm_id].circ = self.circuits[s.circ_id]
      else:
        self.streams[s.strm_id].circ = self.streams[s.strm_id].pending_circ
      self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      self.streams[s.strm_id].pending_circ = None
      self.streams[s.strm_id].attached_at = s.arrived_at
    elif s.status == "FAILED" or s.status == "CLOSED":
      # FIXME stats
      if s.strm_id not in self.streams:
        plog("NOTICE", "Failed stream "+str(s.strm_id)+" not found")
        return

      if not s.circ_id:
        plog("WARN", "Stream "+str(s.strm_id)+" failed from no circuit!")

      # We get failed and closed for each stream. OK to return 
      # and let the closed do the cleanup
      if s.status == "FAILED":
        # Avoid busted circuits that will not resolve or carry
        # traffic. 
        self.streams[s.strm_id].failed = True
        if s.circ_id in self.circuits: self.circuits[s.circ_id].dirty = True
        else: plog("WARN","Failed stream on unknown circ "+str(s.circ_id))
        return

      if self.streams[s.strm_id].pending_circ:
        self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      del self.streams[s.strm_id]
    elif s.status == "REMAP":
      if s.strm_id not in self.streams:
        plog("WARN", "Remap id "+str(s.strm_id)+" not found")
      else:
        if not re.match(r"\d+.\d+.\d+.\d+", s.target_host):
          s.target_host = "255.255.255.255"
          plog("NOTICE", "Non-IP remap for "+str(s.strm_id)+" to "
                   + s.target_host)
        self.streams[s.strm_id].host = s.target_host
        self.streams[s.strm_id].port = s.target_port

  def stream_bw_event(self, s):
    output = [s.event_name, str(s.strm_id), str(s.bytes_read),
              str(s.bytes_written)]
    plog("DEBUG", " ".join(output))
    if not s.strm_id in self.streams:
      plog("WARN", "BW event for unknown stream id: "+str(s.strm_id))
    else:
      self.streams[s.strm_id].bytes_read += s.bytes_read
      self.streams[s.strm_id].bytes_written += s.bytes_written
 
  def ns_event(self, n):
    self.read_routers(n.nslist)
    plog("DEBUG", "Read " + str(len(n.nslist))+" NS => " 
       + str(len(self.sorted_r)) + " routers")
  
  def new_desc_event(self, d):
    for i in d.idlist: # Is this too slow?
      self.read_routers(self.c.get_network_status("id/"+i))
    plog("DEBUG", "Read " + str(len(d.idlist))+" Desc => " 
       + str(len(self.sorted_r)) + " routers")

  def bandwidth_event(self, b): pass # For heartbeat only..

################### CircuitHandler #############################

class CircuitHandler(PathBuilder):
  """ CircuitHandler that extends from PathBuilder """
  def __init__(self, c, selmgr, num_circuits, RouterClass):
    PathBuilder.__init__(self, c, selmgr, RouterClass)
    self.num_circuits = num_circuits    # Size of the circuit pool
    self.check_circuit_pool()	        # Bring up the pool of circs
    
  def check_circuit_pool(self):
    """ Init or check the status of the circuit-pool """
    # Get current number of circuits
    n = len(self.circuits.values())
    i = self.num_circuits-n
    if i > 0:
      plog("INFO", "Checked pool of circuits: we need to build " + 
         str(i) + " circuits")
    # Schedule (num_circs-n) circuit-buildups
    while (n < self.num_circuits):      
      self.build_circuit("255.255.255.255", 80)
      plog("DEBUG", "Scheduled circuit No. " + str(n+1))
      n += 1

  def build_circuit(self, host, port):
    """ Build a circuit """
    circ = None
    while circ == None:
      try:
        self.selmgr.set_target(host, port)
        circ = self.c.build_circuit(self.selmgr.pathlen, 
           self.selmgr.path_selector)
        self.circuits[circ.circ_id] = circ
        return circ
      except TorCtl.ErrorReply, e:
        # FIXME: How come some routers are non-existant? Shouldn't
        # we have gotten an NS event to notify us they disappeared?
        plog("NOTICE", "Error building circuit: " + str(e.args))

  def close_circuit(self, id):
    """ Close a circuit with given id """
    # TODO: Pass streams to another circ before closing?
    self.circuits[id].closed = True
    try: self.c.close_circuit(id)
    except TorCtl.ErrorReply, e: 
      plog("ERROR", "Failed closing circuit " + str(id) + ": " + str(e))

  def circ_status_event(self, c):
    """ Handle circuit status events """
    output = [c.event_name, str(c.circ_id), c.status]
    if c.path: output.append(",".join(c.path))
    if c.reason: output.append("REASON=" + c.reason)
    if c.remote_reason: output.append("REMOTE_REASON=" + c.remote_reason)
    plog("DEBUG", " ".join(output))
    
    # Circuits we don't control get built by Tor
    if c.circ_id not in self.circuits:
      plog("DEBUG", "Ignoring circuit " + str(c.circ_id) + 
         " (controlled by Tor)")
      return
    
    # EXTENDED
    if c.status == "EXTENDED":
      # Compute elapsed time
      extend_time = c.arrived_at-self.circuits[c.circ_id].last_extended_at
      self.circuits[c.circ_id].extend_times.append(extend_time)
      plog("INFO", "Circuit " + str(c.circ_id) + " extended in " + 
         str(extend_time) + " sec")
      self.circuits[c.circ_id].last_extended_at = c.arrived_at
    
    # FAILED & CLOSED
    elif c.status == "FAILED" or c.status == "CLOSED":
      # XXX: Can still get a STREAM FAILED for this circ after this
      circ = self.circuits[c.circ_id]
      # Actual removal of the circ
      del self.circuits[c.circ_id]
      # Give away pending streams
      for stream in circ.pending_streams:
	plog("DEBUG", "Finding new circ for " + str(stream.strm_id))
        self.attach_stream_any(stream, stream.detached_from)
      # Check if there are enough circs
      self.check_circuit_pool()
      return
    
    # BUILT
    elif c.status == "BUILT":
      circ = self.circuits[c.circ_id]
      circ.built = True
      for stream in circ.pending_streams:
        try:
          self.c.attach_stream(stream.strm_id, c.circ_id)
        except TorCtl.ErrorReply, e:
          # No need to retry here. We should get the failed
          # event for either the circ or stream next
          plog("WARN", "Error attaching stream: " + str(e.args))
      # Compute duration by summing up extend_times
      duration = reduce(lambda x, y: x+y, circ.extend_times, 0.0)
      plog("INFO", "Circuit " + str(c.circ_id) + " needed " + 
         str(duration) + " seconds to be built")
      # Save the duration to the circuit for later use
      circ.setup_duration = duration
      
    # OTHER?
    else:
      # If this was e.g. a LAUNCHED
      pass

################### StreamHandler ##############################

class StreamHandler(CircuitHandler):
  """ StreamHandler that extends from the CircuitHandler """
  def __init__(self, c, selmgr, num_circs, RouterClass):
    CircuitHandler.__init__(self, c, selmgr, num_circs, RouterClass)
    self.sorted_circs = None    # optional sorted list

  def clear_dns_cache(self):
    """ Send signal CLEARDNSCACHE """
    lines = self.c.sendAndRecv("SIGNAL CLEARDNSCACHE\r\n")
    for _, msg, more in lines:
      plog("DEBUG", "CLEARDNSCACHE: " + msg)

  def close_stream(self, id, reason):
    """ Close a stream with given id and reason """
    self.c.close_stream(id, reason)

  def create_and_attach(self, stream, unattached_streams):
    """ Create a new circuit and attach (stream + unattached_streams) """
    circ = self.build_circuit(stream.host, stream.port)
    if circ:
      for u in unattached_streams:
        plog("DEBUG", "Attaching " + str(u.strm_id) + 
           " pending build of circuit " + str(circ.circ_id))
        u.pending_circ = circ      
      circ.pending_streams.extend(unattached_streams)
      self.circuits[circ.circ_id] = circ
      self.last_exit = circ.exit
 
  def attach_stream_any(self, stream, badcircs):
    """ Attach a regular user stream """
    unattached_streams = [stream]
    if self.new_nym:
      self.new_nym = False
      plog("DEBUG", "Obeying new nym")
      for key in self.circuits.keys():
        if (not self.circuits[key].dirty
            and len(self.circuits[key].pending_streams)):
          plog("WARN", "New nym called, destroying circuit "+str(key)
             +" with "+str(len(self.circuits[key].pending_streams))
             +" pending streams")
          unattached_streams.extend(self.circuits[key].pending_streams)
          del self.circuits[key].pending_streams[:]
        # FIXME: Consider actually closing circs if no streams
        self.circuits[key].dirty = True

    # Check if there is a sorted list of circs
    if self.sorted_circs: list = self.sorted_circs
    else: list = self.circuits.values()
    for circ in list:
      # Check each circuit
      if circ.built and not circ.closed and circ.circ_id not in badcircs and not circ.dirty:
        if circ.exit.will_exit_to(stream.host, stream.port):
          try:
            self.c.attach_stream(stream.strm_id, circ.circ_id)
            stream.pending_circ = circ # Only one possible here
            circ.pending_streams.append(stream)
            self.last_exit = circ.exit
          except TorCtl.ErrorReply, e:
            # No need to retry here. We should get the failed
            # event for either the circ or stream next
            plog("WARN", "Error attaching stream: " + str(e.args))
            return
          break
	else:
	  plog("DEBUG", "Circuit " + str(circ.circ_id) + " won't exit")
    else:
      self.create_and_attach(stream, unattached_streams)

  def stream_status_event(self, s):
    """ Catch user stream events """
    # Construct debugging output
    output = [s.event_name, str(s.strm_id), s.status, str(s.circ_id), s.target_host, str(s.target_port)]
    if s.reason: output.append("REASON=" + s.reason)
    if s.remote_reason: output.append("REMOTE_REASON=" + s.remote_reason)
    plog("DEBUG", " ".join(output))
     
    # If target_host is not an IP-address
    if not re.match(r"\d+.\d+.\d+.\d+", s.target_host):
      s.target_host = "255.255.255.255" # ignore DNS for exit policy check
    
    # NEW or NEWRESOLVE
    if s.status == "NEW" or s.status == "NEWRESOLVE":
      if s.status == "NEWRESOLVE" and not s.target_port:
        s.target_port = self.resolve_port      
      # Set up the new stream
      stream = Stream(s.strm_id, s.target_host, s.target_port, s.status)
      self.streams[s.strm_id] = stream        
      self.attach_stream_any(self.streams[s.strm_id], self.streams[s.strm_id].detached_from)
    
    # DETACHED
    elif s.status == "DETACHED":
      # Stream not found
      if s.strm_id not in self.streams:
        plog("WARN", "Detached stream " + str(s.strm_id) + " not found")
        self.streams[s.strm_id] = Stream(s.strm_id, s.target_host, s.target_port, "NEW")
      # Circuit not found
      if not s.circ_id:
        plog("WARN", "Stream " + str(s.strm_id) + " detached from no circuit!")
      else:
        self.streams[s.strm_id].detached_from.append(s.circ_id)      
      # Detect timeouts on user streams
      if s.reason == "TIMEOUT":
	# TODO: Count timeouts on streams?
	#self.streams[s.strm_id].timeout_counter += 1
	plog("DEBUG", "User stream timed out on circuit " + str(s.circ_id))
      # Stream was pending
      if self.streams[s.strm_id] in self.streams[s.strm_id].pending_circ.pending_streams:
        self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      # Attach to another circ
      self.streams[s.strm_id].pending_circ = None
      self.attach_stream_any(self.streams[s.strm_id], self.streams[s.strm_id].detached_from)

    # SUCCEEDED
    if s.status == "SUCCEEDED":
      if s.strm_id not in self.streams:
        plog("NOTICE", "Succeeded stream " + str(s.strm_id) + " not found")
        return
      if s.circ_id and self.streams[s.strm_id].pending_circ.circ_id != s.circ_id:
        # Hrmm.. this can happen on a new-nym.. Very rare, putting warn
        # in because I'm still not sure this is correct
        plog("WARN", "Mismatch of pending: "
          + str(self.streams[s.strm_id].pending_circ.circ_id) + " vs "
          + str(s.circ_id))
	self.streams[s.strm_id].circ = self.circuits[s.circ_id]
      else:
        self.streams[s.strm_id].circ = self.streams[s.strm_id].pending_circ
      self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      self.streams[s.strm_id].pending_circ = None
      self.streams[s.strm_id].attached_at = s.arrived_at

    # FAILED or CLOSED
    elif s.status == "FAILED" or s.status == "CLOSED":
      if s.strm_id not in self.streams:
        plog("NOTICE", "Failed stream " + str(s.strm_id) + " not found")
        return
      # if not s.circ_id: 
      # plog("WARN", "Stream " + str(s.strm_id) + " closed/failed from no circuit")
      # We get failed and closed for each stream, let CLOSED do the cleanup
      if s.status == "FAILED":
        # Avoid busted circuits that will not resolve or carry traffic
        self.streams[s.strm_id].failed = True
	if s.circ_id in self.circuits: self.circuits[s.circ_id].dirty = True
        elif self.streams[s.strm_id].attached_at != 0: 
	  plog("WARN", "Failed stream on unknown circuit " + str(s.circ_id))
	return
      # CLOSED
      if self.streams[s.strm_id].pending_circ:
        self.streams[s.strm_id].pending_circ.pending_streams.remove(self.streams[s.strm_id])
      # Actual removal of the stream
      del self.streams[s.strm_id]

    # REMAP
    elif s.status == "REMAP":
      if s.strm_id not in self.streams:
        plog("WARN", "Remap id "+str(s.strm_id)+" not found")
      else:
        if not re.match(r"\d+.\d+.\d+.\d+", s.target_host):
          s.target_host = "255.255.255.255"
          plog("NOTICE", "Non-IP remap for "+str(s.strm_id) + 
             " to " + s.target_host)		   
        self.streams[s.strm_id].host = s.target_host
        self.streams[s.strm_id].port = s.target_port

########################## Unit tests ##########################

def do_unit(rst, r_list, plamb):
  print "\n"
  print "-----------------------------------"
  print rst.r_is_ok.im_class
  above_i = 0
  above_bw = 0
  below_i = 0
  below_bw = 0
  for r in r_list:
    if rst.r_is_ok(r):
      print r.nickname+" "+plamb(r)+"="+str(rst.r_is_ok(r))+" "+str(r.bw)
      if r.bw > 400000:
        above_i = above_i + 1
        above_bw += r.bw
      else:
        below_i = below_i + 1
        below_bw += r.bw
        
  print "Routers above: " + str(above_i) + " bw: " + str(above_bw)
  print "Routers below: " + str(below_i) + " bw: " + str(below_bw)

# TODO: Tests:
#  - Test each NodeRestriction and print in/out lines for it
#  - Test NodeGenerator and reapply NodeRestrictions
#  - Same for PathSelector and PathRestrictions
#  - Also Reapply each restriction by hand to path. Verify returns true

if __name__ == '__main__':
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect(("127.0.0.1",9061))
  c = Connection(s)
  c.debug(file("control.log", "w"))
  c.authenticate()
  nslist = c.get_network_status()
  sorted_rlist = c.read_routers(c.get_network_status())

  sorted_rlist.sort(lambda x, y: cmp(y.bw, x.bw))
  for i in xrange(len(sorted_rlist)): sorted_rlist[i].list_rank = i
  
  for r in sorted_rlist:
    if r.will_exit_to("211.11.21.22", 465):
      print r.nickname+" "+str(r.bw)

  do_unit(FlagsRestriction(["Guard"], []), sorted_rlist, lambda r: " ".join(r.flags))
  do_unit(FlagsRestriction(["Fast"], []), sorted_rlist, lambda r: " ".join(r.flags))
  exit(0)

  do_unit(ExitPolicyRestriction("2.11.2.2", 80), sorted_rlist,
          lambda r: "exits to 80")
  do_unit(PercentileRestriction(0, 100, sorted_rlist), sorted_rlist,
          lambda r: "")
  do_unit(PercentileRestriction(10, 20, sorted_rlist), sorted_rlist,
          lambda r: "")
  do_unit(OSRestriction([r"[lL]inux", r"BSD", "Darwin"], []), sorted_rlist,
          lambda r: r.os)
  do_unit(OSRestriction([], ["Windows", "Solaris"]), sorted_rlist,
          lambda r: r.os)
   
  do_unit(VersionRangeRestriction("0.1.2.0"), sorted_rlist,
          lambda r: str(r.version))
  do_unit(VersionRangeRestriction("0.1.2.0", "0.1.2.5"), sorted_rlist,
          lambda r: str(r.version))
  do_unit(VersionIncludeRestriction(["0.1.1.26-alpha", "0.1.2.7-ignored"]),
          sorted_rlist, lambda r: str(r.version))
  do_unit(VersionExcludeRestriction(["0.1.1.26"]), sorted_rlist,
          lambda r: str(r.version))

  do_unit(ConserveExitsRestriction(), sorted_rlist, lambda r: " ".join(r.flags))
  do_unit(FlagsRestriction([], ["Valid"]), sorted_rlist, lambda r: " ".join(r.flags))

  do_unit(IdHexRestriction("$FFCB46DB1339DA84674C70D7CB586434C4370441"),
          sorted_rlist, lambda r: r.idhex)

  rl =  [AtLeastNNodeRestriction([ExitPolicyRestriction("255.255.255.255", 80), ExitPolicyRestriction("255.255.255.255", 443), ExitPolicyRestriction("255.255.255.255", 6667)], 2), FlagsRestriction([], ["BadExit"])]

  exit_rstr = NodeRestrictionList(rl)

  ug = UniformGenerator(sorted_rlist, exit_rstr)

  rlist = []
  for r in ug.next_r():
    print "Checking: " + r.nickname
    for rs in rl:
      if not rs.r_is_ok(r):
        raise PathError()
      if not "Exit" in r.flags:
        print "No exit in flags of "+r.nickname
    rlist.append(r)
  for r in sorted_rlist:
    if "Exit" in r.flags and not r in rlist:
      print r.nickname+" is an exit not in rl!"
        
