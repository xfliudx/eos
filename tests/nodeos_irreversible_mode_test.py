#!/usr/bin/env python3

from testUtils import Utils
from Cluster import Cluster
from WalletMgr import WalletMgr
from Node import Node
from Node import ReturnType
from TestHelper import TestHelper
from testUtils import Account

import re
import os
import time
import signal
import subprocess
import shutil


Print = Utils.Print
errorExit = Utils.errorExit
cmdError = Utils.cmdError

args = TestHelper.parse_args({"-v"})
Utils.Debug = args.v

walletMgr=WalletMgr(True)
cluster=Cluster(walletd=True)
cluster.walletMgr = walletMgr

relaunchTimeout = 5

def getLatestStdErrFile(nodeId):
   dataDir = Cluster.getDataDir(nodeId)
   latestStdErrFile = None
   pattern = re.compile("stderr\..*\.txt")
   for item in os.listdir(dataDir):
      if pattern.match(item) and (latestStdErrFile is None or latestStdErrFile < item):
         latestStdErrFile = item
   if latestStdErrFile is None:
      latestStdErrFile = "stderr.txt"
   return os.path.join(dataDir, latestStdErrFile)

def checkStdErr(nodeId, message):
   latestStdErrFile = getLatestStdErrFile(nodeId)
   with open(latestStdErrFile, 'r') as f:
      for line in f:
         if message in line:
            return True
   return False

def removeReversibleBlocks(nodeId):
   dataDir = Cluster.getDataDir(nodeId)
   reversibleBlocks = os.path.join(dataDir, "blocks", "reversible")
   shutil.rmtree(reversibleBlocks, ignore_errors=True)

def assertBugExists(condition, bugId):
   assert(condition)
   print("#{} BUG CONFIRMED".format(bugId))

def getHeadBlockNumAndLib(node):
   info = nodeToTest.getInfo()
   headBlockNum = int(info["head_block_num"])
   lib = int(info["last_irreversible_block_num"])
   return headBlockNum, lib

try:
   TestHelper.printSystemInfo("BEGIN")
   cluster.killall(allInstances=True)
   cluster.cleanup()
   cluster.launch(prodCount=4, totalNodes=4, pnodes=1, totalProducers=4, useBiosBootFile=False)
   # Give some time for it to produce, so lib is advancing
   time.sleep(60)

   # Stop producing node, so we have stable lib and head block num
   producingNode = cluster.getNode(0)
   producingNode.kill(signal.SIGTERM)
   time.sleep(5)

   # 1st test case: Replay in irreversible mode with reversible blocks
   # Bug: duplicate block added error

   nodeIdOfNodeToTest = 1
   nodeToTest = cluster.getNode(nodeIdOfNodeToTest)

   # Kill node and replay in irreversible mode
   nodeToTest.kill(signal.SIGTERM)
   isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible --replay", timeout=relaunchTimeout)

   isStdErrContainMessage = False
   if not isRelaunchSuccess:
      isStdErrContainMessage = checkStdErr(nodeIdOfNodeToTest, "duplicate block added")

   # TODO: Change this to proper assert
   if not isRelaunchSuccess and isStdErrContainMessage:
      Print("!!!BUG #1 CONFIRMED")
   else:
      Print("!!!BUG #1 IS NOT CONFIRMED")


   # 2nd test case: Replay in irreversible mode without reversible blocks
   # Bug: last_irreversible_block != the real lib (e.g. if lib is 1000, it replays up to 1000 saying head is 1000 and lib is 999)

   nodeIdOfNodeToTest = 2
   nodeToTest = cluster.getNode(nodeIdOfNodeToTest)
   # Track head block num and lib before shutdown
   headBlockNumBeforeShutdown, libBeforeShutdown = getHeadBlockNumAndLib(nodeToTest)
   # Shut node, remove reversible blocks and relaunch
   nodeToTest.kill(signal.SIGTERM)
   removeReversibleBlocks(nodeIdOfNodeToTest)
   isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible --replay", timeout=relaunchTimeout)
   assert(isRelaunchSuccess)
   # Wait some time for replay to finish
   time.sleep(10)
   # Check head block num and lib
   headBlockNum, lib = getHeadBlockNumAndLib(nodeToTest)

   # TODO: Change this to proper assert
   Print("headBlockNumBeforeShutdown {}, headBlockNum {}, lib {}, libBeforeShutdown {}".format(headBlockNumBeforeShutdown, headBlockNum, lib, libBeforeShutdown))
   if not (headBlockNum == libBeforeShutdown and lib == libBeforeShutdown):
      Print("!!!BUG #2 CONFIRMED")
   else:
      Print("!!!BUG #2 IS NOT CONFIRMED")

   # 3rd test case: Switch mode speculative -> irreversible -> speculative without replay
   nodeIdOfNodeToTest = 3
   nodeToTest = cluster.getNode(nodeIdOfNodeToTest)
   headBlockNumBeforeShutdown, libBeforeShutdown = getHeadBlockNumAndLib(nodeToTest)
   nodeToTest.kill(signal.SIGTERM)
   isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "--read-mode irreversible", timeout=relaunchTimeout)
   assert(isRelaunchSuccess)
   nodeToTest.kill(signal.SIGTERM)
   isRelaunchSuccess = nodeToTest.relaunch(nodeIdOfNodeToTest, "", timeout=relaunchTimeout, addOrSwapFlags={"--read-mode": "speculative"})
   headBlockNum, lib = getHeadBlockNumAndLib(nodeToTest)

   # TODO: Change this to proper assert
   Print("headBlockNumBeforeShutdown {}, headBlockNum {}, lib {}, libBeforeShutdown {}".format(headBlockNumBeforeShutdown, headBlockNum, lib, libBeforeShutdown))
   assert(isRelaunchSuccess)
finally:
   TestHelper.shutdown(cluster, walletMgr)
   print("finally")

exit(0)
